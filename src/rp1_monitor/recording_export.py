"""Lossless PG point export. No aggregation, DDL, recorder or device operations."""

import csv
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .factory_bridge import private_file, timestamp
from .platform_client import NativePlatformError

SCHEMA = "rp1-monitor-recording-v1"
MAX_METADATA_BYTES = 256 * 1024
MAX_LINE_BYTES = 2 * 1024**2
MAX_EXPORT_BYTES = 50 * 1024**3
MAX_EXPORT_DIRECTORY_BYTES = 64 * 1024**3
MIN_FREE_BYTES = 512 * 1024**2
PREFIX = ["time_s", "sample", "rp1_recording_json", "rp1_sample_json"]


def cancelled(event):
    if event is not None and event.is_set():
        raise NativePlatformError("cancelled")


def iso(value):
    result = value if isinstance(value, datetime) else timestamp(value)
    if result is None or result.tzinfo is None:
        raise NativePlatformError("invalid_recording_time")
    return result.isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def pg_url(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise NativePlatformError("postgres_config_must_be_private_0600")
    url = private_file(path, 16384).decode("utf-8").strip()
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise NativePlatformError("postgres_only")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


class ResearchRecordingExporter:
    def __init__(self, directory, *, max_export_bytes=MAX_EXPORT_BYTES,
                 max_directory_bytes=MAX_EXPORT_DIRECTORY_BYTES, min_free_bytes=MIN_FREE_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if self.directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NativePlatformError("invalid_export_directory")
        for value in (max_export_bytes, max_directory_bytes, min_free_bytes):
            if type(value) is not int or value < 0:
                raise NativePlatformError("invalid_export_budget")
        self.max_export_bytes = max_export_bytes
        self.max_directory_bytes = max_directory_bytes
        self.min_free_bytes = min_free_bytes

    def _engine(self, postgres_file):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool
        return create_engine(pg_url(postgres_file), poolclass=NullPool, connect_args={"connect_timeout": 5})

    def recordings(self, postgres_file, *, robot_id=None, limit=200, cancel_event=None):
        from sqlalchemy import func, select, text

        from .recordings import samples, sessions

        limit = min(200, max(1, int(limit)))
        engine = self._engine(postgres_file)
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL statement_timeout='5000ms'"))
                    query = select(sessions).where(sessions.c.ended_at.is_not(None), sessions.c.source_kind == "ros2")
                    if robot_id:
                        query = query.where(sessions.c.robot_id == robot_id)
                    result = []
                    for row in connection.execute(query.order_by(sessions.c.started_at.desc(), sessions.c.id.desc()).limit(limit)).mappings():
                        cancelled(cancel_event)
                        count, high = connection.execute(select(func.count(), func.max(samples.c.sequence)).where(samples.c.recording_id == row["id"])).one()
                        result.append({**dict(row), "recording_id": row["id"], "started_at": iso(row["started_at"]),
                                       "ended_at": iso(row["ended_at"]), "sample_count": count, "high_water_sequence": high})
                    return result
        finally:
            engine.dispose()

    def export(self, postgres_file, recording_id, *, robot_id=None, cancel_event=None):
        from sqlalchemy import func, select, text

        from .recordings import samples, sessions

        UUID(str(recording_id))
        engine = self._engine(postgres_file)
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL statement_timeout='5000ms'"))
                    row = connection.execute(select(sessions).where(sessions.c.id == str(recording_id))).mappings().first()
                    if row is None or (robot_id and row["robot_id"] != robot_id):
                        raise NativePlatformError("recording_not_found")
                    count, high = connection.execute(select(func.count(), func.max(samples.c.sequence)).where(samples.c.recording_id == row["id"])).one()
                    query = select(samples.c.sequence, samples.c.observed_at, samples.c.payload, samples.c.payload_hash).where(samples.c.recording_id == row["id"], samples.c.sequence <= (high or 0)).order_by(samples.c.sequence)
                    stream = connection.execution_options(stream_results=True, max_row_buffer=256).execute(query).mappings()
                    try:
                        return self.export_rows(dict(row), stream, int(count), high, cancel_event=cancel_event)
                    finally:
                        stream.close()
        finally:
            engine.dispose()

    def export_rows(self, recording, rows, count, high, *, cancel_event=None):
        """Pure streaming encoder also used by offline fixtures; rows are exact points."""
        # Serialize quota check + staging + rename across native service processes.
        quota = self.directory / ".quota.sqlite"
        if quota.is_symlink():
            raise NativePlatformError("invalid_export_directory")
        with closing(sqlite3.connect(quota, timeout=1)) as lock:
            os.chmod(quota, 0o600)
            lock.execute("CREATE TABLE IF NOT EXISTS export_lock(id INTEGER PRIMARY KEY)")
            lock.commit()
            lock.execute("BEGIN IMMEDIATE")
            existing_bytes = 0
            for root, directories, files in os.walk(self.directory, followlinks=False):
                if any((Path(root) / item).is_symlink() for item in [*directories, *files]):
                    raise NativePlatformError("invalid_export_directory")
                existing_bytes += sum((Path(root) / item).stat().st_size for item in files)
            return self._export_rows(recording, rows, count, high, existing_bytes, cancel_event=cancel_event)

    def _export_rows(self, recording, rows, count, high, existing_bytes, *, cancel_event=None):
        if recording.get("source_kind") != "ros2":
            raise NativePlatformError("research_requires_real_source")
        if not recording.get("ended_at"):
            raise NativePlatformError("recording_not_ended")
        start, end = timestamp(iso(recording["started_at"])), timestamp(iso(recording["ended_at"]))
        if end < start or type(count) is not int or count <= 0 or type(high) is not int or high <= 0:
            raise NativePlatformError("invalid_recording_snapshot")
        metrics = recording.get("metrics")
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 256 or any(not isinstance(key, str) or not key or len(key) > 120 for key in metrics) or len(set(metrics)) != len(metrics) or set(metrics) & set(PREFIX):
            raise NativePlatformError("invalid_recording_metrics")
        meta = {"schema_version": SCHEMA, "recording_id": recording.get("id", recording.get("recording_id")),
                "robot_id": recording["robot_id"], "source_kind": "ros2", "started_at": iso(recording["started_at"]),
                "ended_at": iso(recording["ended_at"]), "metrics": metrics, "source_context": recording.get("source_context", {}),
                "sample_count": count, "high_water_sequence": high}
        for name in ("run_number", "case_number", "robot_number", "site", "frequency_hz"):
            if name in recording:
                meta[name] = recording[name]
        metadata = canonical(meta)
        if len(metadata.encode()) > MAX_METADATA_BYTES:
            raise NativePlatformError("recording_metadata_too_large")
        export_id = str(uuid4())
        staging = Path(tempfile.mkdtemp(prefix=".export-", dir=self.directory))
        destination = self.directory / export_id
        digest, evidence = hashlib.sha256(), hashlib.sha256()
        size = seen = previous_sequence = gaps = 0
        previous_time = None
        try:
            csv_path = staging / "raw.csv"
            with os.fdopen(os.open(csv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                def write(values):
                    nonlocal size
                    buffer = io.StringIO(newline="")
                    csv.writer(buffer, lineterminator="\n").writerow(values)
                    encoded = buffer.getvalue().encode("utf-8")
                    if len(encoded) > MAX_LINE_BYTES or size + len(encoded) > self.max_export_bytes:
                        raise NativePlatformError("recording_export_size_limit")
                    if existing_bytes + size + len(encoded) + MAX_METADATA_BYTES + 8192 > self.max_directory_bytes:
                        raise NativePlatformError("recording_export_directory_full")
                    if shutil.disk_usage(self.directory).free - len(encoded) - MAX_METADATA_BYTES - 8192 < self.min_free_bytes:
                        raise NativePlatformError("recording_export_disk_reserve")
                    file.write(encoded)
                    digest.update(encoded)
                    size += len(encoded)

                write(PREFIX + metrics)
                for row in rows:
                    cancelled(cancel_event)
                    sequence, payload = row["sequence"], row["payload"]
                    if type(sequence) is not int or sequence <= previous_sequence or sequence > high or not isinstance(payload, dict):
                        raise NativePlatformError("invalid_source_sequence")
                    try:
                        payload_hash = hashlib.sha256(canonical(payload).encode()).hexdigest()
                    except (ValueError, TypeError):
                        raise NativePlatformError("invalid_source_payload") from None
                    if payload_hash != row["payload_hash"]:
                        raise NativePlatformError("source_payload_hash_mismatch")
                    observed = iso(row["observed_at"])
                    if timestamp(payload.get("observed_at")) != timestamp(observed):
                        raise NativePlatformError("source_observation_time_mismatch")
                    elapsed = (timestamp(observed) - start).total_seconds()
                    if elapsed < 0 or timestamp(observed) > end or (previous_time is not None and elapsed <= previous_time):
                        raise NativePlatformError("non_monotonic_source_time")
                    values = payload.get("values")
                    if not isinstance(values, dict) or type(payload.get("connected")) is not bool or payload.get("time_basis") != "pc_sampled":
                        raise NativePlatformError("invalid_source_payload")
                    if payload.get("source_kind") != "ros2" and not (
                        payload.get("source_kind") is None and payload.get("connected") is False and all(value is None for value in values.values())
                    ):
                        raise NativePlatformError("research_requires_real_source")
                    numeric = []
                    for key in metrics:
                        value = values.get(key)
                        if value is None:
                            numeric.append("")
                        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e100:
                            raise NativePlatformError("invalid_source_numeric_value")
                        else:
                            numeric.append(repr(value))
                    sample = canonical({"sequence": sequence, "observed_at": observed, "payload": payload, "payload_hash": payload_hash})
                    if len(sample.encode()) > MAX_METADATA_BYTES:
                        raise NativePlatformError("sample_metadata_too_large")
                    write([repr(elapsed), sequence, metadata if seen == 0 else "", sample, *numeric])
                    evidence.update(f"{sequence}:{payload_hash}\n".encode())
                    gaps += max(0, sequence - previous_sequence - 1)
                    previous_sequence, previous_time = sequence, elapsed
                    seen += 1
                if seen != count or previous_sequence != high:
                    raise NativePlatformError("source_snapshot_count_mismatch")
                file.flush()
                os.fsync(file.fileno())
            cancelled(cancel_event)
            manifest = {"export_id": export_id, "schema_version": SCHEMA, "recording_id": meta["recording_id"],
                        "robot_id": meta["robot_id"], "source_kind": "ros2", "sample_count": count,
                        "high_water_sequence": high, "sequence_gap_count": gaps, "sha256": digest.hexdigest(), "size_bytes": size,
                        "source_evidence_sha256": evidence.hexdigest(), "recording": meta,
                        "exported_at": datetime.now(timezone.utc).isoformat(), "filename": f"monitor-{meta['recording_id']}.csv"}
            with os.fdopen(os.open(staging / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
                json.dump(manifest, file, ensure_ascii=False, allow_nan=False, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.rename(staging, destination)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            manifest.update(path=str(destination / "raw.csv"), manifest_path=str(destination / "manifest.json"))
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
