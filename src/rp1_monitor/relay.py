"""Upper-computer receiver/uploader. The robot has no central network dependency."""

import json
import shutil
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import HTTPException

from .board_diagnostics import BoardJournal
from .health_view import build_health
from .models import (
    BatchAck,
    LiveObservation,
    PreviewFrame,
    RelayStatus,
    TelemetryBatch,
    TransferAck,
    TransferPage,
    TransferReceipt,
    metric_max_age_ms,
)
from .operations_client import validate_url
from .preview_api import create_preview_app
from .process_monitor import build_process_view
from .profiles import RobotProfile
from .spool import Spool, window_digest


@dataclass
class RelaySettings:
    robot_id: str
    source_kind: str
    collector_url: str
    backend_url: str
    token: str = field(repr=False)
    state_dir: Path = Path("runtime/relay")
    ca_file: str | None = None
    cache_bytes: int = 1024**3
    reserve_bytes: int = 256 * 1024**2


def read_json(client, method, url, **kwargs):
    with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("机器人传输超过2MiB限制")
        return json.loads(body)


class RelayRuntime:
    def __init__(self, settings: RelaySettings):
        self.settings = settings
        self.collector_url = validate_url(settings.collector_url, local=True)
        parsed = urlparse(settings.backend_url)
        validate_url(settings.backend_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("设备凭据必须通过HTTPS发送；HTTP仅用于回环演示")
        if settings.source_kind not in {"simulated", "ros2"}:
            raise ValueError("上位机必须绑定明确的数据来源")
        self._tls_verify = (
            ssl.create_default_context(cafile=settings.ca_file) if settings.ca_file else True
        )
        self.spool = Spool(
            settings.state_dir / "pending.sqlite",
            settings.cache_bytes,
            settings.reserve_bytes,
            discard_oldest=False,
        )
        self.spool.bind(settings.robot_id, settings.source_kind)
        self.role = "upper_computer"
        self.profile = RobotProfile(robot_id=settings.robot_id)
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._transfer_lock = threading.RLock()
        self._preview = None
        self._received_at = None
        self.current_stream_id = None
        self.current_sequence = 0
        self.receiver_error = "等待机器人网线连接或SSH隧道"
        self.collector_error = ""
        self.last_error = ""
        self.last_warning = ""
        self.last_upload = None
        self.backfill_error = ""
        self._prefer_backfill = False
        self._backfill_retry_at = 0.0
        self._receive_backfill = False
        self._live_observation = None
        self._live_observed_monotonic = None
        self.threads = []
        self.status_session = uuid4()
        self.status_sequence = 0
        self.status_error = ""
        self._monitoring_configuration = None
        self.configuration_error = "尚未同步中央机型与温度配置"
        self.configuration_synced_at = None
        self.board_journal = BoardJournal(settings.state_dir / "board-diagnostics.sqlite")
        self.board_error = ""

    def start(self):
        self.threads = [
            threading.Thread(target=self.receive, name="robot-receive", daemon=True),
            threading.Thread(target=self.upload, name="central-upload", daemon=True),
            threading.Thread(target=self.report_status, name="relay-status", daemon=True),
            threading.Thread(
                target=self.transfer_board_reports, name="board-report-transfer", daemon=True
            ),
        ]
        for thread in self.threads:
            thread.start()

    def check_identity(self, robot_id, source_kind):
        if (robot_id, source_kind) != (self.settings.robot_id, self.settings.source_kind):
            raise ValueError("机器人ID或来源与上位机绑定不一致")

    def receive_once(self, client, *, transfer=True):
        state_started = time.monotonic()
        frame = PreviewFrame.model_validate(
            read_json(client, "GET", self.collector_url + "/api/v1/state")
        )
        transit_ms = (time.monotonic() - state_started) * 1000
        frame.ages_ms = {key: value + transit_ms for key, value in frame.ages_ms.items()}
        frame.topic_ages_ms = {
            key: value + transit_ms if value is not None else None
            for key, value in frame.topic_ages_ms.items()
        }
        frame.processes = [
            row.model_copy(
                update={"age_ms": row.age_ms + transit_ms if row.age_ms is not None else None}
            )
            for row in frame.processes
        ]
        self.check_identity(frame.robot_id, frame.source_kind)
        profile = (
            RobotProfile.model_validate(frame.device_profile)
            if frame.device_profile
            else RobotProfile(robot_id=frame.robot_id)
        )
        if profile.robot_id != self.settings.robot_id:
            raise ValueError("机器人档案身份不匹配")
        with self._lock:
            self._preview = frame.model_dump(mode="json")
            self._received_at = time.monotonic()
            self.profile = profile
            self.collector_error = frame.upload.get("collector_error", "")
        if not transfer:
            return
        delivery = "backfill" if self._receive_backfill else "live"
        self._receive_backfill = not self._receive_backfill
        request_started = time.monotonic()
        page = TransferPage.model_validate(
            read_json(
                client,
                "GET",
                self.collector_url + "/api/v1/transfer/windows",
                params={"delivery": delivery},
            )
        )
        request_finished = time.monotonic()
        relay_observed_at = datetime.now(timezone.utc)
        self.check_identity(page.robot_id, page.source_kind)
        if page.delivery != delivery or page.stream_id != frame.stream_id:
            raise ValueError("采集实例在传输期间变化；下次重新读取")
        with self._transfer_lock:
            for window in page.windows:
                self.check_identity(window.robot_id, window.source_kind)
                self.spool.append(window)  # FULL synchronous commit happens before robot ACK.
            current_window = next(
                (window for window in page.windows if window.sequence == page.sequence), None
            )
            if page.sampled_at and current_window is not None:
                window = current_window
                duration_ms = (request_finished - request_started) * 1000
                if window.sequence == page.sequence and duration_ms <= 3000:
                    observation = LiveObservation(
                        stream_id=window.stream_id,
                        sequence=window.sequence,
                        collector_sampled_at=page.sampled_at,
                        relay_observed_at=relay_observed_at,
                        round_trip_ms=duration_ms,
                    )
                    from .transport_clock import observation_elapsed

                    if (
                        observation_elapsed(observation, window, observation.relay_observed_at)
                        is not None
                    ):
                        self._live_observation = observation
                        self._live_observed_monotonic = request_finished
            self.current_stream_id = page.stream_id
            self.current_sequence = page.sequence
        if page.windows:
            ack = TransferAck(
                robot_id=self.settings.robot_id,
                receipts=[
                    TransferReceipt(
                        stream_id=window.stream_id,
                        sequence=window.sequence,
                        payload_sha256=window_digest(window),
                    )
                    for window in page.windows
                ],
            )
            read_json(
                client,
                "POST",
                self.collector_url + "/api/v1/transfer/receipts",
                json=ack.model_dump(mode="json"),
            )

    def receive(self):
        next_transfer = 0.0
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            while not self.stop_event.is_set():
                try:
                    transfer = time.monotonic() >= next_transfer
                    if transfer:
                        next_transfer = time.monotonic() + 0.25
                    self.receive_once(client, transfer=transfer)
                    if transfer:
                        self.receiver_error = ""
                    self.stop_event.wait(0.1)
                except (httpx.HTTPError, ValueError, OSError):
                    self.receiver_error = "机器人接收/落盘未完成；检查网线、SSH隧道、绑定身份及上位机磁盘，未确认数据留在机器人"
                    self.stop_event.wait(0.5)

    def _next_upload_batch(self):
        # Backfill can arrive after a live window on the PC; insertion order is not source order.
        newest = self.spool.newest(self.current_stream_id) or self.spool.newest()
        if newest is None:
            return
        observation = self._live_observation
        evidence_fresh = (
            observation is not None
            and (observation.stream_id, observation.sequence) == (newest.stream_id, newest.sequence)
            and self._live_observed_monotonic is not None
            and 0 <= time.monotonic() - self._live_observed_monotonic < 3
        )
        fresh = (
            newest.stream_id == self.current_stream_id
            and newest.sequence == self.current_sequence
            and (
                evidence_fresh
                or 0 <= (datetime.now(timezone.utc) - newest.window_end).total_seconds() < 3
            )
        )
        backfill_ready = time.monotonic() >= self._backfill_retry_at
        if not fresh and not backfill_ready:
            return
        send_live = fresh and not (
            backfill_ready and self._prefer_backfill and self.spool.status()["pending_windows"] > 1
        )
        windows = [newest] if send_live else self.spool.oldest()
        batch = TelemetryBatch(
            delivery="live" if send_live else "backfill",
            windows=windows,
            live_observation=observation if send_live and evidence_fresh else None,
        )
        return batch

    def upload_once(self, client: httpx.Client):
        with self._transfer_lock:
            batch = self._next_upload_batch()
        if batch is None:
            return
        windows = batch.windows
        while len(batch.model_dump_json().encode()) > 1024 * 1024:
            if len(windows) <= 1:
                raise ValueError("单条摘要超过中央传输限制；待传数据保留")
            windows = windows[: max(1, len(windows) // 2)]
            batch = TelemetryBatch(delivery="backfill", windows=windows)
        # Rotate attempts as well as successes; one rejected historical batch
        # must never monopolize the upload loop and freeze latest state.
        self._prefer_backfill = batch.delivery == "live"
        response = client.post(
            f"{self.settings.backend_url.rstrip('/')}/api/v1/robot-telemetry/batches",
            content=batch.model_dump_json(),
            headers={
                "Authorization": f"Bearer {self.settings.token}",
                "Content-Type": "application/json",
            },
        )
        if batch.delivery == "backfill" and response.status_code in {409, 422}:
            self._backfill_retry_at = time.monotonic() + 30
            self.backfill_error = (
                "历史批次被拒绝，保留待传数据并稍后重试；当前实时上报继续，请检查映射或冲突"
            )
        response.raise_for_status()
        ack = BatchAck.model_validate(response.json())
        if not ack.receipts:
            raise ValueError("服务器没有确认任何记录")
        self.spool.acknowledge(windows, ack.receipts)
        if batch.delivery == "backfill":
            self.backfill_error = ""
            self._backfill_retry_at = 0.0
        self.last_upload = datetime.now(timezone.utc).isoformat()
        self.last_error = ""
        self.last_warning = (
            "资料已入库，但实时状态未更新：检查设备时钟或旧采集实例" if ack.live_ignored else ""
        )

    def upload(self):
        failures = 0
        with httpx.Client(
            timeout=3, verify=self._tls_verify, trust_env=False, follow_redirects=False
        ) as client:
            while not self.stop_event.is_set():
                try:
                    self.upload_once(client)
                    failures = 0
                    self.stop_event.wait(0.2)
                except (httpx.HTTPError, ValueError, OSError):
                    failures += 1
                    self.last_error = "上报失败，未确认数据保留在本地；检查服务、凭据或证书"
                    self.stop_event.wait(min(30, 2 ** min(failures, 5)))

    def status_message(self):
        status = self.spool.status()
        with self._lock:
            connected = self._received_at is not None and time.monotonic() - self._received_at <= 1
            robot = self._preview.get("upload", {}) if self._preview and connected else {}
        self.status_sequence += 1
        try:
            free = shutil.disk_usage(self.settings.state_dir).free
        except OSError:
            free = None
        return RelayStatus(
            robot_id=self.settings.robot_id,
            source_kind=self.settings.source_kind,
            session_id=self.status_session,
            sequence=self.status_sequence,
            observed_at=datetime.now(timezone.utc),
            collector_connected=connected,
            pending_windows=status["pending_windows"],
            pending_bytes=status["pending_bytes"],
            cache_budget_bytes=status["cache_payload_budget_bytes"],
            free_disk_bytes=free,
            reserve_bytes=self.settings.reserve_bytes,
            robot_pending_windows=robot.get("pending_windows"),
            robot_free_disk_bytes=robot.get("free_disk_bytes"),
            robot_reserve_bytes=robot.get("reserve_bytes"),
            robot_observed_at=self._preview.get("sampled_at")
            if self._preview and connected
            else None,
            robot_disk_observed_at=robot.get("disk_observed_at"),
        )

    def report_status(self):
        with httpx.Client(
            timeout=3, verify=self._tls_verify, trust_env=False, follow_redirects=False
        ) as client:
            while not self.stop_event.is_set():
                try:
                    response = client.post(
                        self.settings.backend_url.rstrip("/")
                        + "/api/v1/robot-telemetry/relay-status",
                        json=self.status_message().model_dump(mode="json"),
                        headers={"Authorization": f"Bearer {self.settings.token}"},
                    )
                    response.raise_for_status()
                    self.status_error = ""
                except (httpx.HTTPError, ValueError, OSError):
                    self.status_error = "上位机运行状态上报失败，中央缓存和磁盘告警可能未知"
                try:
                    self.sync_configuration(client)
                except (httpx.HTTPError, ValueError, OSError):
                    self.configuration_error = (
                        "中央配置同步失败；本机沿用最近已确认版本，当前中央版本未知"
                    )
                self.stop_event.wait(5)

    def sync_configuration(self, client):
        from .monitor_config import MonitoringConfiguration

        envelope = read_json(
            client,
            "GET",
            self.settings.backend_url.rstrip("/") + "/api/v1/robot-telemetry/configuration",
            headers={"Authorization": f"Bearer {self.settings.token}"},
        )
        self.check_identity(envelope.get("robot_id"), envelope.get("source_kind"))
        if envelope.get("source") not in {"database", "local_file", "unconfigured"}:
            raise ValueError("中央配置来源无效")
        if envelope["source"] == "database":
            if type(envelope.get("revision")) is not int or envelope["revision"] < 1:
                raise ValueError("中央配置版本无效")
            configuration = MonitoringConfiguration.model_validate(envelope["configuration"])
            configuration.validate_source(self.settings.source_kind)
            envelope = {
                key: envelope.get(key) for key in ("robot_id", "source_kind", "revision", "source")
            }
            envelope["configuration"] = configuration.model_dump(mode="json")
        with self._lock:
            self._monitoring_configuration = envelope
            self.configuration_synced_at = datetime.now(timezone.utc).isoformat()
            self.configuration_error = ""

    def board_status(self):
        try:
            with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
                status = read_json(client, "GET", self.collector_url + "/api/v1/board-diagnostics")
            self.check_identity(status.get("robot_id"), status.get("source_kind"))
            if "checks" not in status:
                status["checks"] = self.board_journal.checks()
            return {**status, "upload_error": self.board_error}
        except (httpx.HTTPError, ValueError, OSError):
            return {
                "robot_id": self.settings.robot_id,
                "source_kind": self.settings.source_kind,
                "enabled": False,
                "items": [],
                "latest": self.board_journal.latest(),
                "checks": self.board_journal.checks(),
                "detail": "板端检查接口不可达；可查看本机保存的历史报告，旧采集版本需更新",
                "upload_error": self.board_error,
            }

    def start_board_run(self, body):
        # Called only by the local native client; central/viewer APIs never proxy execution.
        try:
            with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
                return read_json(
                    client, "POST", self.collector_url + "/api/v1/board-diagnostics/runs", json=body
                )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise HTTPException(
                code if code in {409, 422, 429, 503} else 502,
                "板端检查请求未接受；请刷新状态，核对任务、安装和检查间隔",
            ) from exc
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise HTTPException(502, "板端检查请求未确认；先刷新状态，避免重复发起") from exc

    def receive_board_reports(self, client):
        page = read_json(
            client,
            "GET",
            self.collector_url + "/api/v1/board-diagnostics/reports",
            params={"after": self.board_journal.cursor()["after"]},
        )
        self.board_journal.receive_page(page, self.settings.robot_id, self.settings.source_kind)

    def upload_board_reports(self, client):
        for report in self.board_journal.pending():
            ack = read_json(
                client,
                "POST",
                self.settings.backend_url.rstrip("/") + "/api/v1/robot-telemetry/board-diagnostics",
                json=report,
                headers={"Authorization": f"Bearer {self.settings.token}"},
            )
            if ack.get("report_id") != report["report_id"]:
                raise ValueError("中央确认的报告 ID 不一致")
            self.board_journal.acknowledge(ack["report_id"], ack.get("sha256"))

    def transfer_board_reports(self):
        with (
            httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as collector,
            httpx.Client(
                timeout=3, verify=self._tls_verify, trust_env=False, follow_redirects=False
            ) as central,
        ):
            while not self.stop_event.is_set():
                errors = []
                try:
                    self.receive_board_reports(collector)
                except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                    errors.append("板端报告读取失败或旧采集端不支持；已有遥测继续")
                try:
                    self.upload_board_reports(central)
                except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                    errors.append("板端报告中央上传未确认，本机保留并重试")
                self.board_error = "；".join(errors)
                self.stop_event.wait(5)

    def snapshot(self):
        with self._lock:
            if self._preview is None:
                raise HTTPException(503, "上位机尚未收到绑定机器人的预览；请检查网线及SSH隧道")
            frame = PreviewFrame.model_validate(self._preview)
            # Ignore any collector-supplied field; only the host contacts central.
            frame.monitoring_configuration = self._monitoring_configuration
            age_ms = max(0, (time.monotonic() - self._received_at) * 1000)
        frame.ages_ms = {key: age + age_ms for key, age in frame.ages_ms.items()}
        frame.topic_ages_ms = {
            key: age + age_ms if age is not None else None
            for key, age in frame.topic_ages_ms.items()
        }
        connected = age_ms <= 1000
        frame.processes = [
            row.model_copy(
                update={"age_ms": row.age_ms + age_ms if row.age_ms is not None else None}
            )
            for row in frame.processes
        ]
        frame.values = {
            key: value
            if connected and frame.ages_ms.get(key, float("inf")) <= metric_max_age_ms(key)
            else None
            for key, value in frame.values.items()
        }
        frame.health = build_health(
            frame.values,
            frame.ages_ms,
            frame.health_sources,
            frame.health_sensors,
            frame.joint_names,
            available=connected,
        )
        frame.health["processes"] = build_process_view(
            frame.processes, frame.values, frame.ages_ms, available=connected
        )
        frame.upload = {
            **self.spool.status(),
            "stage": "upper_computer",
            "collector_connected": connected,
            "robot_pending_windows": frame.upload.get("pending_windows", 0),
            "robot_discarded_windows": frame.upload.get("discarded_windows", 0),
            "last_success_at": self.last_upload,
            "error": self.last_error,
            "collector_error": self.collector_error,
            "receiver_error": self.receiver_error,
            "warning": self.last_warning,
            "backfill_error": self.backfill_error,
            "status_error": self.status_error,
            "central_url": self.settings.backend_url.rstrip("/"),
            "configuration_error": self.configuration_error,
            "configuration_synced_at": self.configuration_synced_at,
            "clock_basis": "relay_round_trip" if self._live_observation else None,
            "clock_offset_seconds": (
                self._live_observation.relay_observed_at
                - self._live_observation.collector_sampled_at
            ).total_seconds()
            if self._live_observation
            else None,
            "clock_uncertainty_ms": self._live_observation.round_trip_ms
            if self._live_observation
            else None,
        }
        return frame

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=4)


def create_relay_app(settings: RelaySettings):
    return create_preview_app(RelayRuntime(settings), "RP1 Upper-computer Relay")
