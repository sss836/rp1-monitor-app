"""Durable bounded upload queue, separate from original ROS recordings."""

import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path

from .models import TelemetryWindow


def window_digest(window: TelemetryWindow) -> str:
    payload = json.dumps(window.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class Spool:
    def __init__(
        self,
        path: Path,
        max_bytes: int = 1024**3,
        min_free_bytes: int = 256 * 1024**2,
        *,
        discard_oldest=True,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.discard_oldest = discard_oldest
        if max_bytes <= 0 or min_free_bytes < 0:
            raise ValueError("缓存容量必须大于0，磁盘保留值不能为负数")
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA wal_autocheckpoint=64;
                PRAGMA journal_size_limit=1048576;
                CREATE TABLE IF NOT EXISTS queue (
                  id INTEGER PRIMARY KEY, stream TEXT NOT NULL, sequence INTEGER NOT NULL,
                  payload TEXT NOT NULL, bytes INTEGER NOT NULL, UNIQUE(stream,sequence));
                CREATE TABLE IF NOT EXISTS losses (
                  id INTEGER PRIMARY KEY, first_window TEXT, last_window TEXT, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS stats (
                  id INTEGER PRIMARY KEY CHECK(id=1), count INTEGER NOT NULL, bytes INTEGER NOT NULL, lost INTEGER NOT NULL);
            """)
            connection.execute(
                "INSERT OR IGNORE INTO stats SELECT 1,(SELECT count(*) FROM queue),(SELECT coalesce(sum(bytes),0) FROM queue),(SELECT coalesce(sum(count),0) FROM losses)"
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def bind(self, robot_id, source_kind):
        """Never reuse a durable queue for another robot or simulated/ROS source."""
        with self._lock, self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS owner (robot TEXT, source TEXT)")
            owner = connection.execute("SELECT robot,source FROM owner").fetchone()
            mismatch = connection.execute(
                "SELECT 1 FROM queue WHERE json_extract(payload,'$.robot_id') != ? "
                "OR json_extract(payload,'$.source_kind') != ? LIMIT 1",
                (robot_id, source_kind),
            ).fetchone()
            if mismatch or owner is not None and owner != (robot_id, source_kind):
                raise ValueError("缓存目录已绑定其他机器人或来源，请使用独立目录")
            if owner is None:
                connection.execute("INSERT INTO owner VALUES (?,?)", (robot_id, source_kind))

    def append(self, window: TelemetryWindow):
        payload = window.model_dump_json()
        size = len(payload.encode())
        with self._lock, self._connect() as connection:
            if shutil.disk_usage(self.path.parent).free < self.min_free_bytes:
                raise OSError("采集磁盘剩余空间低于保留值，停止写入摘要缓存")
            existing = connection.execute(
                "SELECT payload FROM queue WHERE stream=? AND sequence=?",
                (str(window.stream_id), window.sequence),
            ).fetchone()
            if existing:
                if TelemetryWindow.model_validate_json(existing[0]) != window:
                    raise ValueError("同一缓存身份的内容不可改写")
                return
            total = connection.execute("SELECT bytes FROM stats WHERE id=1").fetchone()[0]
            if not self.discard_oldest and total + size > self.max_bytes:
                raise OSError("上位机待传缓存已满；保留已有摘要，不确认机器人端数据")
            connection.execute(
                "INSERT INTO queue(stream,sequence,payload,bytes) VALUES(?,?,?,?)",
                (str(window.stream_id), window.sequence, payload, size),
            )
            connection.execute("UPDATE stats SET count=count+1,bytes=bytes+? WHERE id=1", (size,))
            total = connection.execute("SELECT bytes FROM stats WHERE id=1").fetchone()[0]
            discarded = []
            while total > self.max_bytes:
                row = connection.execute(
                    "SELECT id,payload,bytes FROM queue ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    break
                connection.execute("DELETE FROM queue WHERE id=?", (row[0],))
                connection.execute(
                    "UPDATE stats SET count=count-1,bytes=bytes-?,lost=lost+1 WHERE id=1", (row[2],)
                )
                discarded.append(json.loads(row[1]))
                total -= row[2]
            if discarded:
                connection.execute(
                    "INSERT INTO losses(first_window,last_window,count) VALUES(?,?,?)",
                    (discarded[0]["window_start"], discarded[-1]["window_end"], len(discarded)),
                )
                connection.execute(
                    "DELETE FROM losses WHERE id NOT IN (SELECT id FROM losses ORDER BY id DESC LIMIT 128)"
                )

    def newest(self, stream_id=None) -> TelemetryWindow | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM queue WHERE stream=? ORDER BY sequence DESC LIMIT 1"
                if stream_id is not None
                else "SELECT payload FROM queue ORDER BY id DESC LIMIT 1",
                (str(stream_id),) if stream_id is not None else (),
            ).fetchone()
            return TelemetryWindow.model_validate_json(row[0]) if row else None

    def oldest(self, limit=30) -> list[TelemetryWindow]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM queue ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            return [TelemetryWindow.model_validate_json(row[0]) for row in rows]

    def acknowledge(self, windows: list[TelemetryWindow], receipts):
        sent = {(str(window.stream_id), window.sequence) for window in windows}
        accepted = {(str(receipt.stream_id), receipt.sequence) for receipt in receipts}
        if not accepted.issubset(sent):
            raise ValueError("服务器确认了本次未发送的记录")
        with self._lock, self._connect() as connection:
            for stream, sequence in sorted(accepted):
                row = connection.execute(
                    "DELETE FROM queue WHERE stream=? AND sequence=? RETURNING bytes",
                    (stream, sequence),
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE stats SET count=count-1,bytes=bytes-? WHERE id=1", (row[0],)
                    )

    def acknowledge_transfer(self, receipts):
        """A robot buffer is released only for the exact content durably received by the PC."""
        with self._lock, self._connect() as connection:
            for receipt in receipts:
                key = (str(receipt.stream_id), receipt.sequence)
                row = connection.execute(
                    "SELECT payload,bytes FROM queue WHERE stream=? AND sequence=?", key
                ).fetchone()
                if row is None:  # The PC may retry after a lost acknowledgement response.
                    continue
                if (
                    window_digest(TelemetryWindow.model_validate_json(row[0]))
                    != receipt.payload_sha256
                ):
                    raise ValueError("接收确认的摘要哈希不一致，保留机器人缓存")
                connection.execute("DELETE FROM queue WHERE stream=? AND sequence=?", key)
                connection.execute(
                    "UPDATE stats SET count=count-1,bytes=bytes-? WHERE id=1", (row[1],)
                )

    def status(self) -> dict:
        with self._lock, self._connect() as connection:
            count, size, lost = connection.execute(
                "SELECT count,bytes,lost FROM stats WHERE id=1"
            ).fetchone()
            ranges = [
                dict(zip(("first_window", "last_window", "count"), row))
                for row in connection.execute(
                    "SELECT first_window,last_window,count FROM losses ORDER BY id DESC LIMIT 16"
                )
            ]
            return {
                "pending_windows": count,
                "pending_bytes": size,
                "discarded_windows": lost,
                "cache_payload_budget_bytes": self.max_bytes,
                "recent_loss_ranges": ranges,
            }
