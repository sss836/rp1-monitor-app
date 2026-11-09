"""Independent PC recorders sharing one history store, never a robot control path."""

import threading
from pathlib import Path

from pydantic import TypeAdapter

from .models import RobotId
from .recordings import Recorder, recording_catalog

_robot_id = TypeAdapter(RobotId)


class FleetRecorder:
    """Keep each robot's active job and pending queue separate, including legacy jobs."""

    def __init__(self, store, directory, source, roster, *, allow_sqlite=False):
        self.source, self.roster = source, roster
        self.directory = Path(directory) / "recording-robots"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError("记录缓存目录不能是符号链接")
        self.lock = threading.RLock()
        self._started = self._closed = False
        self._allow_sqlite = allow_sqlite
        self._store = store
        self.recorders = {}
        self._legacy_robot_id = None
        self.legacy = Recorder(
            store, Path(directory) / "recording-outbox.sqlite", self._legacy_source,
            allow_sqlite=allow_sqlite,
        )
        self._all = [self.legacy]
        try:
            legacy_id = self._active_identity(self.legacy)
            if legacy_id:
                self._legacy_robot_id = legacy_id
                self.recorders[legacy_id] = self.legacy
            for path in sorted(self.directory.glob("*.sqlite")):
                identity = _robot_id.validate_python(path.stem)
                recorder = self._open(identity)
                self._all.append(recorder)
                active_id = self._active_identity(recorder)
                if active_id and active_id != identity:
                    raise ValueError("记录缓存与机器人身份不一致，拒绝恢复")
                previous = self.recorders.get(identity)
                if previous and previous.active and recorder.active:
                    raise ValueError("同一机器人存在两份活动记录缓存，需人工核对；未开始采样")
                if not previous or recorder.active:
                    self.recorders[identity] = recorder
        except Exception:
            for recorder in self._all:
                recorder.close()
            raise

    @staticmethod
    def _active_identity(recorder):
        if not recorder.active:
            return None
        identity = _robot_id.validate_python(recorder.active["query"]["robot_id"])
        if recorder.active["session"]["robot_id"] != identity:
            raise ValueError("活动记录的采集身份不一致，拒绝恢复")
        return identity

    def _legacy_source(self):
        identity = self._legacy_robot_id
        return self._frame(identity) if identity else None

    def _frame(self, identity):
        frame = self.source(identity)
        if frame and (identity is None or frame.get("robot_id") == identity):
            return frame
        return None

    def _open(self, identity):
        path = self.directory / f"{_robot_id.validate_python(identity)}.sqlite"
        if path.is_symlink():
            raise ValueError("记录缓存文件不能是符号链接")
        return Recorder(
            self._store, path, lambda: self._frame(identity), allow_sqlite=self._allow_sqlite,
        )

    def _robots(self):
        entries = {row["robot_id"]: dict(row) for row in self.roster()}
        for identity, recorder in self.recorders.items():
            active = recorder.state()["active"]
            entries.setdefault(identity, {
                "robot_id": identity, "connected": False,
                "source_kind": active["session"]["source_kind"] if active else None,
            })
        return list(entries.values())

    def _base_state(self):
        return {**self.legacy.state(), "active": None, "pending_samples": 0,
                "pending_stops": 0, "pending_bytes": 0, "last_capture_at": None, "error": ""}

    def context(self, robot_id=None):
        with self.lock:
            entries = self._robots()
            if robot_id is None and len(entries) == 1:
                robot_id = entries[0]["robot_id"]
            if robot_id is None and not entries:
                frame = self._frame(None)
                if frame:
                    robot_id = frame["robot_id"]
                    entries = [{"robot_id": robot_id, "source_kind": frame["source_kind"],
                                "connected": bool(frame.get("upload", {}).get("collector_connected"))}]
            recorder = self.recorders.get(robot_id)
            state = recorder.state() if recorder else self._base_state()
            frame = self._frame(robot_id) if robot_id else None
            active = state["active"]
            source_kind = (
                frame.get("source_kind") if frame else
                active["session"]["source_kind"] if active else
                next((row.get("source_kind") for row in entries if row["robot_id"] == robot_id), None)
            )
            states = [r.state() for r in self._all]
            return {
                **state, "robot_id": robot_id, "source_kind": source_kind,
                "connected": bool(frame and frame.get("upload", {}).get("collector_connected")),
                "catalog": recording_catalog(frame), "robots": entries,
                "selection_required": robot_id is None and len(entries) > 1,
                "active_recordings": [s["active"] for s in states if s["active"]],
                "total_pending_samples": sum(s["pending_samples"] for s in states),
                "legacy_pending_samples": self.legacy.state()["pending_samples"],
            }

    def state(self, robot_id=None):
        return self.context(robot_id)

    def display_sessions(self, robot_ids):
        """Read only active identity and timing, without scanning recording queues."""
        # Starting a recording can hold these locks across PostgreSQL I/O.
        # Display refreshes skip the unavailable run and keep connectivity.
        if not self.lock.acquire(blocking=False):
            return {}
        try:
            selected = [(robot_id, self.recorders.get(robot_id)) for robot_id in robot_ids]
        finally:
            self.lock.release()
        result = {}
        for robot_id, recorder in selected:
            if recorder is None or not recorder.lock.acquire(blocking=False):
                continue
            try:
                session = (recorder.active or {}).get("session")
                if session:
                    result[robot_id] = {key: session.get(key) for key in (
                        "robot_id", "source_kind", "run_number", "started_at", "ended_at",
                    )}
            finally:
                recorder.lock.release()
        return result

    def start(self, query):
        with self.lock:
            if self._closed:
                raise ValueError("记录后台正在退出，请稍后重试")
            recorder = self.recorders.get(query.robot_id)
            if recorder is None:
                recorder = self._open(query.robot_id)
                self.recorders[query.robot_id] = recorder
                self._all.append(recorder)
                if self._started:
                    recorder.start_thread()
            return recorder.start(query)

    def stop(self, robot_id=None):
        with self.lock:
            candidates = [r for identity, r in self.recorders.items()
                          if (robot_id is None or identity == robot_id) and r.state()["active"]]
            if len(candidates) > 1:
                raise ValueError("多台机器人正在记录，请指定要结束的机器人编号")
            if not candidates:
                raise ValueError("所选机器人没有正在运行的监控记录")
            candidates[0].stop()

    def start_thread(self):
        with self.lock:
            if self._started or self._closed:
                return
            self._started = True
            for recorder in self._all:
                recorder.start_thread()

    def capture(self):
        """Manual sampler hook for isolated tests; live samplers run independently."""
        for recorder in list(self._all):
            recorder.capture()

    def drain(self):
        for recorder in list(self._all):
            recorder.drain()

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            recorders = list(self._all)
        for recorder in recorders:
            recorder.close()
