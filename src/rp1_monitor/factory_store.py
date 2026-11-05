"""PC-local factory evidence, independent of PostgreSQL and transport connections.

Imports never rewrite monitoring recordings. Original packages and successive human
results remain available after disconnection, application restart, and re-testing.
"""

import csv
import hashlib
import html
import json
import math
import os
import shutil
import sqlite3
import stat
import struct
import tempfile
import threading
import zlib
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from .board_check_view import BoardCheckView, board_check_view
from .factory_basic_checks import BASIC_CHECKS, EXTRA_CHECKS, FactoryCheckReport
from .factory_board_profile import BoardTestProfile, default_board_profile, factory_board_view
from .factory_codec import decode_payload
from .factory_key_data import MINIMUM_TEST_SECONDS
from .factory_key_data import SCHEMA as KEY_DATA_SCHEMA
from .factory_models import FACTORY_SEGMENT_SCHEMA, FactorySegmentInfo, FactorySegmentManifest
from .factory_test_catalog import (
    BOARD_CASE_CODES,
    CALIBRATION_CASE_CODES,
    ESTOP_PAIRING_CASE_CODE,
    FUNCTION_CASE_CODES,
    LIGHT_CASE_CODE,
    MOTION_CASE_CODES,
    MOTOR_PARAMETERS_CASE_CODE,
    SUITE_CODE,
    SUITE_NAME,
)
from .models import MetricKey, RobotId, SourceKind, metric_spec
from .robopi_tui import MAX_REPORT_BYTES

MAX_PACKAGE_BYTES = 2 * 1024**3
RESERVE_BYTES = 16 * 1024**2
MAX_ROWS = 2_000_000
MAX_SOC_SCREENSHOT_BYTES = 4 * 1024**2
ITEM_NAMES = {
    1: "镜像、电气与电机参数",
    2: "整机标定",
    3: "标定检查",
    4: "站立、行走、跑步与整机功能",
    5: "特技测试",
}
STATUS_NAMES = {"pass": "通过（人工填写）", "fail": "不通过（人工填写）", "incomplete": "未填写结论 / 待复核"}
STATUS_NAMES["not_applicable"] = "不适用（28轴不测试特技）"
FUNCTIONAL_CHECK_NAMES = {"remote_estop": "远程急停", "battery_hot_swap": "热换电"}
_metric = TypeAdapter(MetricKey)
_robot_id = TypeAdapter(RobotId)
_source_kind = TypeAdapter(SourceKind)
BOARD_RESULTS = {"pass", "fail", "error", "needs_review", "skip"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sn(value):
    if not isinstance(value, str):
        raise ValueError("SN必须是文字")
    value = value.strip()
    if not 1 <= len(value) <= 96 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("SN不能为空、过长或包含控制字符")
    return value


def _item(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in ITEM_NAMES:
        raise ValueError("测试项必须为1至5")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _read_json(path, maximum=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("清单不是普通文件或大小超限")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(descriptor)


def _copy_file(source, destination, expected_bytes=None, expected_sha=None):
    """Read regular files without following symlinks; fsync before publication."""
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PACKAGE_BYTES:
            raise ValueError("证据必须是大小不超过2 GiB的非空普通文件")
        if expected_bytes is not None and info.st_size != expected_bytes:
            raise ValueError("日志字节数与清单不一致")
        if shutil.disk_usage(destination.parent).free < info.st_size + RESERVE_BYTES:
            raise OSError("电脑磁盘空间不足；已有证据保留")
        digest, count = hashlib.sha256(), 0
        with os.fdopen(descriptor, "rb", closefd=False) as incoming, destination.open("xb") as outgoing:
            os.chmod(destination, 0o600)
            while block := incoming.read(1024 * 1024):
                count += len(block)
                if count > MAX_PACKAGE_BYTES:
                    raise ValueError("证据文件大小超限")
                digest.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if count != info.st_size or (expected_sha is not None and digest.hexdigest() != expected_sha):
            raise ValueError("日志SHA256校验失败或复制期间文件发生变化")
        return count, digest.hexdigest()
    finally:
        os.close(descriptor)


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_file(path, expected_bytes, expected_sha):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise ValueError("电脑已保存原件的大小不符；保留现场，不覆盖")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_sha:
            raise ValueError("电脑已保存原件的SHA256不符；保留现场，不覆盖")
    finally:
        os.close(descriptor)


@contextmanager
def _open_evidence(path):
    # No input SQL is executed. Views, triggers and virtual tables are refused below.
    if path.is_symlink() or not path.is_file():
        raise ValueError("本地日志文件缺失或不是普通文件")
    database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    database.execute("PRAGMA trusted_schema=OFF")
    database.execute("PRAGMA query_only=ON")
    if hasattr(database, "setlimit"):
        database.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        database.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        database.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
    try:
        yield database
    finally:
        database.close()


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("日志时间无效")
    timestamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("日志时间必须带时区")
    return value


def _soc_observation(value):
    required = {"session_id", "robot_id", "source_kind", "event", "sampled_at", "received_at",
                "soc", "soc_age_ms", "continuity"}
    optional = {"previous_soc", "voltage", "current", "uptime_seconds"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("SOC观察缺少必填字段或包含未允许字段")
    answer = dict(value)
    if not isinstance(value["session_id"], str):
        raise ValueError("SOC观察会话编号无效")
    answer["session_id"] = str(UUID(value["session_id"]))
    answer["robot_id"] = _robot_id.validate_python(value["robot_id"])
    if value["source_kind"] not in ("ros2", "simulated") or value["event"] not in ("baseline", "change", "final"):
        raise ValueError("SOC观察来源或事件无效")
    for key in ("sampled_at", "received_at"):
        _timestamp(value[key])
    for key in ("soc", "soc_age_ms", *optional):
        number = value.get(key)
        if key in optional and number is None:
            continue
        try:
            finite = type(number) in (int, float) and math.isfinite(number)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("SOC观察必须是有限数值，不能用缺失值冒充测量")
        if ((key in {"soc", "previous_soc"} and not 0 <= number <= 100)
                or (key == "soc_age_ms" and not 0 <= number <= 5000)
                or (key == "uptime_seconds" and number < 0)):
            raise ValueError("SOC观察数值超出允许范围或采样已过期")
    if not isinstance(value["continuity"], str) or len(value["continuity"]) > 200:
        raise ValueError("SOC连续性说明无效或超过200字")
    return answer


def _soc_png(path):
    """Validate bounded PNG bytes without depending on a GUI image decoder."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SOC_SCREENSHOT_BYTES:
            raise ValueError("SOC截图必须是大小不超过4 MiB的非空PNG普通文件")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_SOC_SCREENSHOT_BYTES + 1)
        if len(data) != info.st_size or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("SOC截图不是有效PNG或读取时发生变化")
    finally:
        os.close(descriptor)
    position, chunks, compressed, header, ended = 8, 0, [], None, False
    palette, idat_ended = None, False
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError("SOC截图PNG数据不完整")
        size = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        end = position + size + 12
        if any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind) or kind[2] & 32:
            raise ValueError("SOC截图PNG分块类型无效")
        if end > len(data) or zlib.crc32(data[position + 4:end - 4]) != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("SOC截图PNG分块校验失败")
        body = data[position + 8:end - 4]
        if chunks == 0 and (kind != b"IHDR" or size != 13):
            raise ValueError("SOC截图PNG缺少有效图像头")
        if kind == b"IHDR":
            if header is not None or size != 13:
                raise ValueError("SOC截图PNG图像头无效")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            if idat_ended:
                raise ValueError("SOC截图PNG图像分块不连续")
            compressed.append(body)
        elif kind == b"PLTE":
            if palette is not None or compressed or not 0 < size <= 768 or size % 3:
                raise ValueError("SOC截图PNG调色板无效")
            palette = size // 3
        elif kind == b"IEND":
            if size or end != len(data):
                raise ValueError("SOC截图PNG结束标记无效")
            ended = True
        elif kind[0] & 32 == 0 and kind != b"PLTE":
            raise ValueError("SOC截图PNG包含未知关键分块")
        if compressed and kind != b"IDAT":
            idat_ended = True
        position, chunks = end, chunks + 1
    if not ended or not compressed or header is None:
        raise ValueError("SOC截图PNG缺少图像或结束标记")
    width, height, depth, color, compression, filtering, interlace = header
    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    if (not 0 < width <= 16384 or not 0 < height <= 16384 or depth not in depths.get(color, set())
            or compression or filtering or interlace not in (0, 1)):
        raise ValueError("SOC截图PNG尺寸或编码无效")
    if (color == 3 and (palette is None or palette > 2**depth)) or (color in (0, 4) and palette is not None):
        raise ValueError("SOC截图PNG调色板与颜色编码不符")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if not interlace else [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
                                                (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    rows = []
    for x, y, dx, dy in passes:
        columns = max(0, (width - x + dx - 1) // dx)
        lines = max(0, (height - y + dy - 1) // dy)
        if columns and lines:
            rows.extend([1 + (columns * depth * channels + 7) // 8] * lines)
    expected = sum(rows)
    if expected > 64 * 1024**2:
        raise ValueError("SOC截图PNG解码尺寸超限")
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(compressed), expected + 1)
    except zlib.error as exc:
        raise ValueError("SOC截图PNG图像压缩数据无效") from exc
    if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("SOC截图PNG图像数据长度无效")
    position = 0
    for size in rows:
        if pixels[position] > 4:
            raise ValueError("SOC截图PNG行过滤器无效")
        position += size
    return data


def _sample(row, manifest):
    group, sequence, observed_at, elapsed, raw = row
    if group not in {"motion", "health"} or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("日志采样组或序号无效")
    _timestamp(observed_at)
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("日志单调时间无效")
    payload = decode_payload(raw)
    if not isinstance(payload, dict) or payload.get("source_kind") != manifest["source_kind"]:
        raise ValueError("日志采样来源与清单不一致")
    values = payload.get("values")
    if not isinstance(values, dict) or len(values) > 512:
        raise ValueError("日志指标数量或格式无效")
    for key, value in values.items():
        _metric.validate_python(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("日志指标必须为有限数值或空值")
    for field in ("units", "missing_reasons", "ages_ms"):
        if not isinstance(payload.get(field, {}), dict):
            raise ValueError("日志单位、缺失原因或新鲜度格式无效")
    if any(not isinstance(unit, str) or len(unit) > 64 for unit in payload.get("units", {}).values()):
        raise ValueError("日志单位格式无效")
    if any(not isinstance(reason, str) or len(reason) > 512 for reason in payload.get("missing_reasons", {}).values()):
        raise ValueError("日志缺失原因格式无效")
    source_at = payload.get("source_at")
    if source_at is not None:
        _timestamp(source_at)
    return payload


def _validate_database(path, manifest):
    with _open_evidence(path) as database:
        objects = database.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if {name for kind, name, _ in objects} != {"metadata", "samples", "events"} or any(
            kind != "table" or not sql or "VIRTUAL" in sql.upper() for kind, _, sql in objects
        ):
            raise ValueError("日志数据库结构无效，不接受视图、触发器或虚拟表")
        columns = {
            "metadata": ["key", "value"],
            "samples": ["group_name", "sequence", "observed_at", "elapsed_seconds", "payload"],
            "events": ["sequence", "observed_at", "payload"],
        }
        for table, expected in columns.items():
            if [row[1] for row in database.execute(f"PRAGMA table_info({table})")] != expected:
                raise ValueError("日志数据库列与协议不一致")
        if database.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ValueError("日志数据库完整性检查失败")
        metadata = dict(database.execute("SELECT key,value FROM metadata LIMIT 20"))
        if json.loads(metadata.get("schema_version", "null")) != FACTORY_SEGMENT_SCHEMA:
            raise ValueError("日志数据库版本不支持")
        saved = FactorySegmentInfo.model_validate_json(metadata.get("segment", "null")).model_dump(mode="json")
        info = {key: value for key, value in manifest.items() if key not in {"bytes", "sha256", "data_file"}}
        if saved != info:
            raise ValueError("日志内部身份或状态与清单不一致")
        counts = {"motion": 0, "health": 0}
        valid_counts, missing_counts = dict(counts), {}
        previous = {"motion": 0, "health": 0}
        for row in database.execute("SELECT group_name,sequence,observed_at,elapsed_seconds,payload FROM samples ORDER BY group_name,sequence"):
            payload = _sample(row, manifest)
            if row[1] <= previous[row[0]]:
                raise ValueError("日志采样序号重复")
            previous[row[0]] = row[1]
            counts[row[0]] += 1
            valid_counts[row[0]] += int(any(value is not None for value in payload["values"].values()))
            for key in payload.get("missing_reasons", {}):
                missing_counts[key] = missing_counts.get(key, 0) + 1
            if sum(counts.values()) > MAX_ROWS:
                raise ValueError("日志采样数量超限")
        if counts != manifest["sample_counts"]:
            raise ValueError("日志样本数与清单不一致")
        if valid_counts != manifest["valid_counts"] or missing_counts != manifest["missing_counts"]:
            raise ValueError("日志有效/缺失计数与清单不一致")
        events, previous = 0, 0
        for sequence, observed_at, payload in database.execute("SELECT sequence,observed_at,payload FROM events ORDER BY sequence"):
            if not isinstance(sequence, int) or sequence <= previous:
                raise ValueError("日志事件序号无效")
            previous = sequence
            _timestamp(observed_at)
            if not isinstance(json.loads(payload), dict):
                raise ValueError("日志事件格式无效")
            events += 1
            if events > MAX_ROWS:
                raise ValueError("日志事件数量超限")
        if events != manifest["event_count"]:
            raise ValueError("日志事件数与清单不一致")


class FactoryStore:
    """Own only files in a dedicated PC directory; never delete robot/source files."""

    validate_sn = staticmethod(_sn)

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.packages = self.directory / "segments"
        self.attachments = self.directory / "attachments"
        for path in (self.packages, self.attachments):
            if path.is_symlink():
                raise ValueError("本地证据目录不能是符号链接")
            path.mkdir(exist_ok=True, mode=0o700)
        index = self.directory / "factory-index.sqlite"
        if index.is_symlink():
            raise ValueError("本地索引不能是符号链接")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(index, check_same_thread=False)
        os.chmod(index, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS segments (
                segment_id TEXT PRIMARY KEY, sn TEXT NOT NULL, item INTEGER NOT NULL,
                manifest TEXT NOT NULL, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results (
                result_id TEXT PRIMARY KEY, sn TEXT NOT NULL, item INTEGER NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY, sn TEXT NOT NULL, item INTEGER NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS board_snapshots (
                sn TEXT PRIMARY KEY, robot_id TEXT NOT NULL, source_kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS board_evidence (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                plugin_id TEXT NOT NULL, report_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(sn,plugin_id,report_id));
            CREATE TABLE IF NOT EXISTS board_settings (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS board_assessments (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                sha256 TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(sn,sha256));
            CREATE TABLE IF NOT EXISTS calibration_checks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                robot_id TEXT NOT NULL, run_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                payload TEXT NOT NULL, UNIQUE(sn,run_id,sha256));
            CREATE TABLE IF NOT EXISTS light_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                observed_at TEXT NOT NULL, lit INTEGER NOT NULL CHECK(lit IN (0,1)));
            CREATE TABLE IF NOT EXISTS calibration_confirmations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                observed_at TEXT NOT NULL, completed INTEGER NOT NULL CHECK(completed IN (0,1)));
            CREATE TABLE IF NOT EXISTS sn_models (
                sn TEXT PRIMARY KEY, dof INTEGER NOT NULL CHECK(dof IN (24,28)));
            CREATE TABLE IF NOT EXISTS estop_pairing_checks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS functional_checks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL,
                kind TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS soc_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE,
                sn TEXT NOT NULL, attachment_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS soc_observations_no_update BEFORE UPDATE ON soc_observations
                BEGIN SELECT RAISE(ABORT, 'SOC observations are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS soc_observations_no_delete BEFORE DELETE ON soc_observations
                BEGIN SELECT RAISE(ABORT, 'SOC observations are append-only'); END;
        """)
        from .factory_result_summary import initialize

        initialize(self)

    def report_rows(self, sn):
        from .factory_result_summary import report_rows

        return report_rows(self, sn)

    def save_report_edits(self, sn, edits, station_id=""):
        from .factory_result_summary import save_report_edits

        return save_report_edits(self, sn, edits, station_id)

    def report_settings(self):
        from .factory_result_summary import report_settings

        return report_settings(self)

    def export_result_summary(self, sn, output_dir):
        from .factory_result_summary import export_result_summary

        return export_result_summary(self, sn, output_dir)

    def model_dof(self, sn):
        with self.lock:
            row = self.db.execute("SELECT dof FROM sn_models WHERE sn=?", (_sn(sn),)).fetchone()
            if row:
                return row["dof"]
            calibration = self.calibration_check(sn)
            return calibration["report"]["dof"] if calibration else 24

    def set_model_dof(self, sn, dof):
        sn = _sn(sn)
        if type(dof) is not int or dof not in (24, 28):
            raise ValueError("机型必须是24或28轴")
        with self.lock:
            calibration = self.calibration_check(sn)
            if calibration and calibration["report"]["dof"] != dof:
                raise ValueError("机型与本SN已保存的标定检查不一致")
            with self.db:
                self.db.execute("INSERT INTO sn_models VALUES(?,?) ON CONFLICT(sn) DO UPDATE SET dof=excluded.dof", (sn, dof))

    def close(self):
        with self.lock:
            self.db.close()

    def _segment(self, segment_id):
        segment_id = str(UUID(str(segment_id)))
        row = self.db.execute("SELECT * FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
        if row is None:
            raise ValueError("本机尚未完整导入这段日志")
        return {**json.loads(row["manifest"]), "imported_at": row["imported_at"]}

    def list_segments(self, sn):
        sn = _sn(sn)
        with self.lock:
            rows = self.db.execute("SELECT * FROM segments WHERE sn=? ORDER BY imported_at,segment_id", (sn,)).fetchall()
            return [{**json.loads(row["manifest"]), "imported_at": row["imported_at"]} for row in rows]

    def segment_summary(self, segment_id):
        with self.lock:
            manifest = self._segment(segment_id)
            with _open_evidence(self.packages / manifest["segment_id"] / "segment.sqlite") as database:
                metadata = dict(database.execute("SELECT key,value FROM metadata WHERE key IN ('recording_duration_seconds','key_data_summary')"))
                maximum = database.execute("SELECT MAX(elapsed_seconds) FROM samples").fetchone()[0]
            duration = json.loads(metadata["recording_duration_seconds"]) if "recording_duration_seconds" in metadata else maximum
            if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration)
                                         or duration < 0 or (maximum is not None and duration < maximum)):
                raise ValueError("日志记录时长无效，不能判断是否超过2小时")
            summary = decode_payload(metadata["key_data_summary"]) if "key_data_summary" in metadata else {}
            if summary:
                if (summary.get("schema_version") != KEY_DATA_SCHEMA or summary.get("duration_seconds") != duration
                        or not isinstance(summary.get("metrics"), dict)):
                    raise ValueError("关键数据统计格式或记录时长不一致")
                for key, stat in summary["metrics"].items():
                    if (not isinstance(key, str) or not isinstance(stat, dict)
                            or any(type(stat.get(k)) is not int or stat[k] < 0 for k in ("observations", "valid", "missing"))
                            or stat["valid"] + stat["missing"] != stat["observations"]):
                        raise ValueError("关键数据有效和缺失次数不一致")
                    for name in ("minimum", "maximum", "mean", "maximum_age_ms", "maximum_valid_gap_seconds"):
                        value = stat.get(name)
                        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                            raise ValueError("关键数据统计含无效数值")
            return {**summary, "duration_seconds": duration, "minimum_test_seconds": MINIMUM_TEST_SECONDS,
                    "duration_satisfied": duration is not None and duration > MINIMUM_TEST_SECONDS,
                    "recording_mode": summary.get("recording_mode", "legacy_raw"),
                    "performance_verdict": "unverified"}

    def item_duration_satisfied(self, sn, item):
        segments = [row for row in self.list_segments(sn) if (5 if row["test_type"] == "stunt" else 4) == item]
        return bool(segments) and all(self.segment_summary(row["segment_id"])["duration_satisfied"] for row in segments)

    def save_motion_evidence(self, sn, item):
        sn, item = _sn(sn), _item(item)
        if item not in (4, 5) or (item == 5 and self.model_dof(sn) == 28):
            raise ValueError("请选择适用的第4项或第5项；28轴特技不适用")
        with self.lock:
            rows = [row for row in self.list_segments(sn) if (5 if row["test_type"] == "stunt" else 4) == item]
            if not rows:
                raise ValueError(f"请先读取本SN第{item}项的机器人数据，再保存到测试结果")
            records = [dict(segment_id=row["segment_id"], robot_id=row["robot_id"], source_kind=row["source_kind"],
                            test_type=row["test_type"], started_at=row["started_at"], ended_at=row["ended_at"],
                            valid_counts=row["valid_counts"], summary=self.segment_summary(row["segment_id"])) for row in rows]
            body = _json(dict(sn=sn, item=item, records=records, performance_verdict="unverified"))
            digest = hashlib.sha256(body.encode()).hexdigest()
            name = f"item{item}-{digest}.json"
            directory = self.directory / "motion-results"
            directory.mkdir(exist_ok=True, mode=0o700)
            path = directory / name
            if not path.exists():
                with path.open("x", encoding="utf-8") as stream:
                    os.chmod(path, 0o600)
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(directory)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("已有关键数据摘要未完整保存，请核对电脑文件；未关联错误附件")
            existing = [json.loads(row["payload"]) for row in self.db.execute(
                "SELECT payload FROM attachments WHERE sn=? AND item=?", (sn, item))]
            attachment = next((row for row in existing if row["name"] == name and row["sha256"] == digest), None)
            attachment = attachment or self.add_attachment(sn, item, path)
            previous = self.results(sn)[item - 1]["latest"] or {}
            ids = [row["segment_id"] for row in rows]
            enough = all(row["summary"]["duration_satisfied"] for row in records)
            if attachment["attachment_id"] not in previous.get("attachment_ids", []) or previous.get("segment_ids") != ids:
                self.save_result(sn, item, "incomplete", f"已归档{len(ids)}段关键数据；" +
                                 ("各段记录超过2小时，待填写实际结论。" if enough else "存在时长不足的记录，未标记完成。"), ids,
                                 [*previous.get("attachment_ids", []), attachment["attachment_id"]], completed=False)
            return dict(segment_count=len(ids), duration_satisfied=enough)

    def _check_identity(self, sn, robot_id, source_kind):
        previous_check = self.db.execute("SELECT robot_id FROM calibration_checks WHERE sn=? LIMIT 1", (sn,)).fetchone()
        if previous_check and (previous_check["robot_id"] != robot_id or source_kind == "simulated"):
            raise ValueError("该SN已有其他机器人或实机标定检查结果，拒绝混入")
        for previous in self.list_segments(sn):
            if (previous["robot_id"], previous["source_kind"]) != (robot_id, source_kind):
                raise ValueError("该SN已有其他机器人或数据来源的日志，拒绝自动混入")
        previous = self.db.execute("SELECT robot_id,source_kind FROM board_snapshots WHERE sn=?", (sn,)).fetchone()
        if previous and (previous["robot_id"], previous["source_kind"]) != (robot_id, source_kind):
            raise ValueError("该SN已有其他机器人或数据来源的板卡证据，拒绝自动混入")
        previous = self.db.execute("SELECT payload FROM soc_observations WHERE sn=? LIMIT 1", (sn,)).fetchone()
        if previous:
            observation = json.loads(previous["payload"])
            if (observation["robot_id"], observation["source_kind"]) != (robot_id, source_kind):
                raise ValueError("该SN已有其他机器人或数据来源的SOC证据，拒绝自动混入")

    def sn_identity(self, sn):
        sn = _sn(sn)
        with self.lock:
            row = self.db.execute("SELECT robot_id,source_kind FROM board_snapshots WHERE sn=?", (sn,)).fetchone()
            if row:
                return dict(robot_id=row["robot_id"], source_kind=row["source_kind"])
            segments = self.list_segments(sn)
            if segments:
                return {key: segments[0][key] for key in ("robot_id", "source_kind")}
            row = self.db.execute("SELECT payload FROM soc_observations WHERE sn=? ORDER BY sequence LIMIT 1", (sn,)).fetchone()
            if row:
                observation = json.loads(row["payload"])
                return {key: observation[key] for key in ("robot_id", "source_kind")}
            return None

    def save_calibration_check(self, sn, path, expected_robot=None):
        """Save validated measured results without changing the human conclusion."""
        from .manual_calibration_check import validate_report

        sn = _sn(sn)
        data = _read_json(path, maximum=16 * 1024**2)
        if not isinstance(data, dict) or data.get("source_kind") != "hardware":
            raise ValueError("只接受人工标定检查保存的实机结果")
        robot = data.get("robot_id")
        if not isinstance(robot, str) or not robot or (expected_robot and robot != expected_robot):
            raise ValueError("报告机器人身份与本次连接不一致")
        run_id = str(UUID(str(data.get("run_id"))))
        report = data.get("report")
        if not isinstance(report, dict):
            raise ValueError("报告缺少逐关节结果")
        validate_report(report, report.get("dof"))
        if report.get("source", {}).get("plan_mode") != "manual_opposite_zero_limit_check":
            raise ValueError("模拟方案不能作为实机标定检查结果")
        serialized = _json(data)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.lock:
            model = self.db.execute("SELECT dof FROM sn_models WHERE sn=?", (sn,)).fetchone()
            if model and model["dof"] != report["dof"]:
                raise ValueError("标定检查关节数与本SN机型不一致，请核对机型和报告")
            bound = self.sn_identity(sn)
            if bound and (bound["robot_id"] != robot or bound["source_kind"] == "simulated"):
                raise ValueError("该SN已有其他机器人或模拟数据，请先核对")
            previous = self.db.execute("SELECT robot_id FROM calibration_checks WHERE sn=? LIMIT 1", (sn,)).fetchone()
            if previous and previous["robot_id"] != robot:
                raise ValueError("该SN已有其他机器人的标定检查结果")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO calibration_checks(sn,robot_id,run_id,sha256,payload) VALUES(?,?,?,?,?)", (sn, robot, run_id, digest, serialized))
        return {"sn": sn, "robot_id": robot, "run_id": run_id}

    def calibration_check(self, sn):
        with self.lock:
            row = self.db.execute("SELECT payload FROM calibration_checks WHERE sn=? ORDER BY sequence DESC LIMIT 1", (_sn(sn),)).fetchone()
            return json.loads(row["payload"]) if row else None

    def import_segment(self, manifest_path, data_path):
        manifest = FactorySegmentManifest.model_validate(_read_json(manifest_path)).model_dump(mode="json")
        if manifest["state"] == "recording" or manifest["ended_at"] is None:
            raise ValueError("运行中的片段不能导入，请先结束或核对中断状态")
        if manifest["bytes"] > MAX_PACKAGE_BYTES or set(manifest["sample_counts"]) != {"motion", "health"} or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in [*manifest["sample_counts"].values(), *manifest["valid_counts"].values(), *manifest["missing_counts"].values(), manifest["event_count"]]
        ):
            raise ValueError("日志容量或计数无效")
        identifier, sn = manifest["segment_id"], _sn(manifest["sn"])
        with self.lock, tempfile.TemporaryDirectory(prefix=".import-", dir=self.packages) as temporary:
            self._check_identity(sn, manifest["robot_id"], manifest["source_kind"])
            staging = Path(temporary)
            staged_data = staging / "segment.sqlite"
            _copy_file(data_path, staged_data, manifest["bytes"], manifest["sha256"])
            _validate_database(staged_data, manifest)
            destination = self.packages / identifier
            row = self.db.execute("SELECT manifest,imported_at FROM segments WHERE segment_id=?", (identifier,)).fetchone()
            if row:
                if json.loads(row["manifest"]) != manifest:
                    raise ValueError("相同片段ID的内容冲突；已有文件保留，不会覆盖")
                if not destination.is_dir() or destination.is_symlink():
                    raise ValueError("已导入片段的本地文件缺失，请人工核对；不覆盖原记录")
                if _read_json(destination / "manifest.json") != manifest:
                    raise ValueError("电脑已保存清单不符，拒绝覆盖")
                _verify_file(destination / "segment.sqlite", manifest["bytes"], manifest["sha256"])
                return {**manifest, "imported_at": row["imported_at"], "duplicate": True}
            staged_manifest = staging / "manifest.json"
            with staged_manifest.open("x", encoding="utf-8") as stream:
                os.chmod(staged_manifest, 0o600)
                stream.write(_json(manifest))
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(staging)
            if destination.exists():
                # Recover a completed file publication whose index commit was interrupted.
                if destination.is_symlink() or _read_json(destination / "manifest.json") != manifest:
                    raise ValueError("本地已有同ID冲突文件，拒绝覆盖")
                _verify_file(destination / "segment.sqlite", manifest["bytes"], manifest["sha256"])
            else:
                os.rename(staging, destination)
                _sync_directory(self.packages)
            imported_at = _now()
            with self.db:
                self.db.execute("INSERT INTO segments VALUES(?,?,?,?,?)", (identifier, sn, 5 if manifest["test_type"] == "stunt" else 4, _json(manifest), imported_at))
            return {**manifest, "imported_at": imported_at, "duplicate": False}

    def add_attachment(self, sn, item, path):
        sn, item, identifier = _sn(sn), _item(item), str(uuid4())
        with self.lock, tempfile.TemporaryDirectory(prefix=".attachment-", dir=self.attachments) as temporary:
            staging = Path(temporary) / "data"
            count, digest = _copy_file(path, staging)
            target = self.attachments / identifier
            os.rename(staging, target)
            _sync_directory(self.attachments)
            payload = dict(attachment_id=identifier, sn=sn, item=item, name=Path(path).name, bytes=count, sha256=digest, created_at=_now())
            with self.db:
                self.db.execute("INSERT INTO attachments VALUES(?,?,?,?)", (identifier, sn, item, _json(payload)))
            return payload

    def save_result(self, sn, item, status, notes="", segment_ids=None, attachment_ids=None, completed=None):
        sn, item = _sn(sn), _item(item)
        if item == 5 and self.model_dof(sn) == 28:
            raise ValueError("28轴的特技测试不适用，无需填写通过或完成")
        if status == "not_applicable":
            raise ValueError("不适用由机型自动确定，不能手工填写")
        if status not in STATUS_NAMES or not isinstance(notes, str) or len(notes) > 10000:
            raise ValueError("人工结果或备注无效")
        explicit_completed = completed is not None
        if completed is None:
            completed = status != "incomplete"
        if not isinstance(completed, bool):
            raise ValueError("完成标记必须为布尔值")
        segment_ids = list(dict.fromkeys(str(UUID(str(value))) for value in (segment_ids or [])))
        attachment_ids = list(dict.fromkeys(str(UUID(str(value))) for value in (attachment_ids or [])))
        with self.lock:
            for identifier in segment_ids:
                manifest = self._segment(identifier)
                if manifest["sn"] != sn or (5 if manifest["test_type"] == "stunt" else 4) != item:
                    raise ValueError("引用片段的SN或测试项不一致")
            for identifier in attachment_ids:
                row = self.db.execute("SELECT sn,item FROM attachments WHERE attachment_id=?", (identifier,)).fetchone()
                if row is None or row["sn"] != sn or row["item"] != item:
                    raise ValueError("引用附件的SN或测试项不一致")
            if item == 4:
                checks = self.functional_checks(sn)
                if status == "pass" and not all(check["status"] == "pass" for check in checks.values()):
                    raise ValueError("第4项通过须先人工确认远程急停和热换电均通过")
                if not all(check["status"] in {"pass", "fail"} for check in checks.values()):
                    if completed and explicit_completed:
                        raise ValueError("第4项完成须先填写远程急停和热换电的明确人工结论")
                    completed = False
            payload = dict(result_id=str(uuid4()), sn=sn, item=item, status=status, notes=notes, segment_ids=segment_ids, attachment_ids=attachment_ids, created_at=_now(), conclusion_source="human", completed=completed)
            with self.db:
                self.db.execute("INSERT INTO results VALUES(?,?,?,?,?)", (payload["result_id"], sn, item, payload["created_at"], _json(payload)))
            return payload

    def set_completed(self, sn, item, completed):
        sn, item = _sn(sn), _item(item)
        if not isinstance(completed, bool):
            raise ValueError("完成标记必须为布尔值")
        with self.lock:
            current = self.results(sn)[item - 1]
            previous = current["latest"] or {}
            return self.save_result(sn, item, current["status"], previous.get("notes", ""), previous.get("segment_ids", []), previous.get("attachment_ids", []), completed=completed)

    def board_profile(self):
        with self.lock:
            row = self.db.execute("SELECT payload FROM board_settings WHERE key='rp1'").fetchone()
            return (BoardTestProfile.model_validate_json(row["payload"]) if row else default_board_profile()).model_dump(mode="json")

    def save_board_profile(self, values):
        profile = BoardTestProfile.model_validate(values).model_dump(mode="json")
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO board_settings VALUES('rp1',?)", (_json(profile),))
        return profile

    def save_board_snapshot(self, sn, robot_id, source_kind, reports, profile=None):
        sn = _sn(sn)
        robot_id, source_kind = _robot_id.validate_python(robot_id), _source_kind.validate_python(source_kind)
        if not isinstance(reports, dict) or len(reports) > len(BASIC_CHECKS):
            raise ValueError("板卡逐项报告格式或数量无效")
        validated = []
        for plugin_id, report in reports.items():
            if plugin_id not in BASIC_CHECKS or not isinstance(report, dict):
                raise ValueError("板卡项目不在允许的基础检查范围")
            if (report.get("robot_id"), report.get("source_kind")) != (robot_id, source_kind):
                raise ValueError("板卡报告身份或来源不一致")
            if report.get("id", plugin_id) != plugin_id or not isinstance(report.get("result"), str) or report["result"] not in BOARD_RESULTS:
                raise ValueError("板卡逐项编号或结果无效")
            report_id = report.get("report_id")
            if not isinstance(report_id, str):
                raise ValueError("板卡报告编号无效")
            report_id = str(UUID(report_id))
            _timestamp(report.get("created_at"))
            if plugin_id in EXTRA_CHECKS:
                FactoryCheckReport.model_validate({**report, "check_id": plugin_id})
            elif not isinstance(report.get("tui_version"), str) or not 1 <= len(report["tui_version"]) <= 80:
                raise ValueError("板卡报告TUI版本无效")
            if not isinstance(report.get("message"), str) or not isinstance(report.get("details", {}), dict):
                raise ValueError("板卡报告说明或原始详情无效")
            expected_scope = "simulated" if source_kind == "simulated" else "software_observation_not_hardware_acceptance"
            if "validation_scope" in report and report["validation_scope"] != expected_scope:
                raise ValueError("板卡报告验证范围与来源不一致")
            try:
                payload = _json(report)
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("板卡原始报告不是有效JSON") from exc
            if len(payload.encode()) > MAX_REPORT_BYTES:
                raise ValueError("单项板卡报告超过256 KiB限制")
            validated.append((plugin_id, report_id, payload, hashlib.sha256(payload.encode()).hexdigest()))
        with self.lock, self.db:
            self._check_identity(sn, robot_id, source_kind)
            self.db.execute("INSERT OR IGNORE INTO board_snapshots VALUES(?,?,?)", (sn, robot_id, source_kind))
            for plugin_id, report_id, payload, digest in validated:
                old = self.db.execute("SELECT sha256 FROM board_evidence WHERE sn=? AND plugin_id=? AND report_id=?", (sn, plugin_id, report_id)).fetchone()
                if old and old["sha256"] != digest:
                    raise ValueError("同一板卡报告ID内容冲突，已有原始结果保留")
                self.db.execute("INSERT OR IGNORE INTO board_evidence(sn,plugin_id,report_id,sha256,payload) VALUES(?,?,?,?,?)", (sn, plugin_id, report_id, digest, payload))
            snapshot = self.board_snapshot(sn)
            frozen_profile = BoardTestProfile.model_validate(profile or self.board_profile()).model_dump(mode="json")
            heads = {check["id"]: check["latest"] for check in snapshot["checks"] if check["latest"]}
            views = {identity: asdict(factory_board_view(identity, heads.get(identity), frozen_profile, heads)) for identity in BASIC_CHECKS}
            counts = {label: sum(view["label"] == label for view in views.values()) for label in ("通过", "不通过", "未完成", "未检查")}
            assessment = dict(profile=frozen_profile, checks=views, counts=counts,
                              inputs={identity: hashlib.sha256(_json(head).encode()).hexdigest() for identity, head in heads.items()})
            digest = hashlib.sha256(_json(assessment).encode()).hexdigest()
            assessment["assessed_at"] = _now()
            self.db.execute("INSERT OR IGNORE INTO board_assessments(sn,sha256,payload) VALUES(?,?,?)", (sn, digest, _json(assessment)))
            return self.board_snapshot(sn)

    def board_snapshot(self, sn):
        sn = _sn(sn)
        with self.lock:
            binding = self.db.execute("SELECT robot_id,source_kind FROM board_snapshots WHERE sn=?", (sn,)).fetchone()
            if binding is None:
                return None
            history, latest = [], {}
            for row in self.db.execute("SELECT plugin_id,sha256,payload FROM board_evidence WHERE sn=? ORDER BY sequence", (sn,)):
                if hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
                    raise ValueError("本地板卡证据校验失败，拒绝输出不可信结果")
                report = json.loads(row["payload"])
                history.append({**report, "id": row["plugin_id"]})
                latest[row["plugin_id"]] = report
            checks = [dict(id=plugin_id, label=label, latest=latest.get(plugin_id)) for plugin_id, label in BASIC_CHECKS.items()]
            counts = dict.fromkeys(("pass", "fail", "error", "needs_review", "skip", "unchecked"), 0)
            for check in checks:
                counts[check["latest"]["result"] if check["latest"] else "unchecked"] += 1
            row = self.db.execute("SELECT payload FROM board_assessments WHERE sn=? ORDER BY sequence DESC LIMIT 1", (sn,)).fetchone()
            assessment = json.loads(row["payload"]) if row else None
            display_counts = dict.fromkeys(("通过", "不通过", "未完成", "未检查"), 0)
            for check in checks:
                frozen = (assessment or {}).get("checks", {}).get(check["id"])
                label = frozen["label"] if frozen else board_check_view(check["id"], check["latest"]).label
                display_counts[label] += 1
            return dict(robot_id=binding["robot_id"], source_kind=binding["source_kind"], checks=checks, counts=counts, executed=len(latest), total=len(checks), history=history, assessment=assessment, display_counts=display_counts)

    def save_light_observation(self, sn, lit):
        sn = _sn(sn)
        if type(lit) is not bool:
            raise ValueError("亮灯结果必须由人员明确确认")
        observed_at = _now()
        with self.lock, self.db:
            self.db.execute("INSERT INTO light_observations(sn,observed_at,lit) VALUES(?,?,?)", (sn, observed_at, int(lit)))
        return {"sn": sn, "observed_at": observed_at, "lit": lit, "source": "operator"}

    def light_observations(self, sn):
        with self.lock:
            return [{"sn": _sn(sn), "observed_at": row["observed_at"], "lit": bool(row["lit"]), "source": "operator"}
                    for row in self.db.execute("SELECT observed_at,lit FROM light_observations WHERE sn=? ORDER BY sequence", (_sn(sn),))]

    def save_estop_pairing(self, sn, status):
        """Append only an operator answer; never send a pairing or emergency-stop command."""
        sn = _sn(sn)
        if not isinstance(status, str) or status not in {"pass", "fail"}:
            raise ValueError("急停开关对频测试必须由人员明确填写通过或不通过")
        payload = dict(check_id=str(uuid4()), sn=sn, item=1, kind="remote_estop_pairing",
                       case_code=ESTOP_PAIRING_CASE_CODE, status=status, observed_at=_now(),
                       conclusion_source="human")
        with self.lock, self.db:
            self.db.execute("INSERT INTO estop_pairing_checks(sn,observed_at,payload) VALUES(?,?,?)",
                            (sn, payload["observed_at"], _json(payload)))
        return payload

    def estop_pairing(self, sn):
        sn = _sn(sn)
        with self.lock:
            history = [json.loads(row["payload"]) for row in self.db.execute(
                "SELECT payload FROM estop_pairing_checks WHERE sn=? ORDER BY sequence", (sn,))]
            latest = history[-1] if history else None
            return dict(kind="remote_estop_pairing", name="远程急停开关对频测试", item=1,
                        case_code=ESTOP_PAIRING_CASE_CODE, history=history, latest=latest,
                        status=latest["status"] if latest else "incomplete")

    def save_calibration_confirmation(self, sn, completed):
        sn = _sn(sn)
        if type(completed) is not bool:
            raise ValueError("标定状态必须由人员明确确认")
        observed_at = _now()
        payload = dict(result_id=str(uuid4()), sn=sn, item=2, status="incomplete", notes="",
                       segment_ids=[], attachment_ids=[], created_at=observed_at,
                       conclusion_source="human", completed=completed)
        with self.lock, self.db:
            self.db.execute("INSERT INTO calibration_confirmations(sn,observed_at,completed) VALUES(?,?,?)", (sn, observed_at, int(completed)))
            self.db.execute("INSERT INTO results VALUES(?,?,?,?,?)", (payload["result_id"], sn, 2, observed_at, _json(payload)))
        return {"sn": sn, "observed_at": observed_at, "completed": completed, "source": "operator"}

    def calibration_confirmations(self, sn):
        sn = _sn(sn)
        with self.lock:
            return [{"sn": sn, "observed_at": row["observed_at"], "completed": bool(row["completed"]), "source": "operator"}
                    for row in self.db.execute("SELECT observed_at,completed FROM calibration_confirmations WHERE sn=? ORDER BY sequence", (sn,))]

    def save_soc_observation(self, sn, observation, screenshot_path):
        """Append fresh SOC evidence and a verified PC-local PNG; infer no test outcome."""
        sn, observation = _sn(sn), _soc_observation(observation)
        data = _soc_png(screenshot_path)
        identifier = str(uuid4())
        with self.lock, tempfile.TemporaryDirectory(prefix=".soc-", dir=self.attachments) as temporary:
            self._check_identity(sn, observation["robot_id"], observation["source_kind"])
            path = Path(temporary) / f"soc-{identifier}.png"
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            attachment = self.add_attachment(sn, 4, path)
            os.chmod(self.attachments / attachment["attachment_id"], 0o400)
            payload = dict(observation_id=identifier, sn=sn, item=4, created_at=_now(),
                           **observation, attachment_id=attachment["attachment_id"], screenshot=attachment)
            with self.db:
                self.db.execute("INSERT INTO soc_observations(observation_id,sn,attachment_id,payload) VALUES(?,?,?,?)",
                                (identifier, sn, attachment["attachment_id"], _json(payload)))
            return payload

    def soc_observations(self, sn):
        with self.lock:
            return [json.loads(row["payload"]) for row in self.db.execute(
                "SELECT payload FROM soc_observations WHERE sn=? ORDER BY sequence", (_sn(sn),))]

    def save_functional_check(self, sn, kind, status, notes="", segment_ids=None, *, soc_observation_ids=None):
        """Append an operator conclusion; never infer a stop or power-supply transition."""
        sn = _sn(sn)
        if not isinstance(kind, str) or kind not in FUNCTIONAL_CHECK_NAMES:
            raise ValueError("整机功能测试项必须为远程急停或热换电")
        if (not isinstance(status, str) or status not in {"pass", "fail", "incomplete"}
                or not isinstance(notes, str) or len(notes) > 10000):
            raise ValueError("人工功能测试结论或备注无效")
        if segment_ids is not None and not isinstance(segment_ids, (list, tuple)):
            raise ValueError("关联片段必须为列表")
        segment_ids = list(dict.fromkeys(str(UUID(str(value))) for value in (segment_ids or [])))
        if soc_observation_ids is not None and not isinstance(soc_observation_ids, (list, tuple)):
            raise ValueError("关联SOC证据必须为列表")
        soc_observation_ids = list(dict.fromkeys(str(UUID(str(value))) for value in (soc_observation_ids or [])))
        if soc_observation_ids and kind != "battery_hot_swap":
            raise ValueError("SOC观察证据只可关联热换电测试")
        with self.lock:
            for identifier in segment_ids:
                manifest = self._segment(identifier)
                if manifest["sn"] != sn or manifest["test_type"] == "stunt":
                    raise ValueError("功能测试只可关联本SN第4项已导入的片段")
            for identifier in soc_observation_ids:
                row = self.db.execute("SELECT sn FROM soc_observations WHERE observation_id=?", (identifier,)).fetchone()
                if row is None or row["sn"] != sn:
                    raise ValueError("热换电测试只可关联本SN已保存的SOC观察证据")
            payload = dict(check_id=str(uuid4()), sn=sn, item=4, kind=kind, status=status,
                           notes=notes, segment_ids=segment_ids, observed_at=_now(),
                           conclusion_source="human", soc_observation_ids=soc_observation_ids)
            with self.db:
                self.db.execute("INSERT INTO functional_checks(sn,kind,observed_at,payload) VALUES(?,?,?,?)",
                                (sn, kind, payload["observed_at"], _json(payload)))
            return payload

    def functional_checks(self, sn):
        sn = _sn(sn)
        with self.lock:
            rows = self.db.execute("SELECT kind,payload FROM functional_checks WHERE sn=? ORDER BY sequence", (sn,)).fetchall()
            answer = {}
            for kind, name in FUNCTIONAL_CHECK_NAMES.items():
                history = [json.loads(row["payload"]) for row in rows if row["kind"] == kind]
                latest = history[-1] if history else None
                answer[kind] = dict(kind=kind, name=name, history=history, latest=latest,
                                    status=latest["status"] if latest else "incomplete")
            return answer

    def results(self, sn):
        sn = _sn(sn)
        with self.lock:
            rows = self.db.execute("SELECT payload FROM results WHERE sn=? ORDER BY rowid", (sn,)).fetchall()
            history = [json.loads(row["payload"]) for row in rows]
            answer = []
            for item, name in ITEM_NAMES.items():
                entries = [entry for entry in history if entry["item"] == item]
                latest = entries[-1] if entries else None
                completed = latest.get("completed", latest["status"] != "incomplete") if latest else False
                answer.append(dict(item=item, name=name, status=latest["status"] if latest else "incomplete", completed=completed, latest=latest, history=entries, board=self.board_snapshot(sn) if item == 1 else None, lights=self.light_observations(sn) if item == 1 else [], calibration_confirmations=self.calibration_confirmations(sn) if item == 2 else [], calibration=self.calibration_check(sn) if item == 3 else None))
                if item == 1:
                    answer[-1]["estop_pairing"] = self.estop_pairing(sn)
                if item == 4:
                    checks = self.functional_checks(sn)
                    ready = all(check["status"] == "pass" for check in checks.values())
                    performed = all(check["status"] in {"pass", "fail"} for check in checks.values())
                    effective = answer[-1]
                    effective.update(functional_checks=checks, soc_observations=self.soc_observations(sn), functional_checks_satisfied=ready,
                                     functional_checks_completed=performed,
                                     completion_blockers=[check["name"] for check in checks.values() if check["status"] == "incomplete"])
                    if any(check["status"] == "fail" for check in checks.values()):
                        effective["status"] = "fail"
                    elif not ready and effective["status"] != "fail":
                        effective["status"] = "incomplete"
                    effective["completed"] = completed and performed
                if item == 5 and self.model_dof(sn) == 28:
                    answer[-1].update(status="not_applicable", completed=False)
            return answer

    def roster(self):
        with self.lock:
            rows = self.db.execute("SELECT sn FROM segments UNION SELECT sn FROM results UNION SELECT sn FROM attachments UNION SELECT sn FROM board_snapshots UNION SELECT sn FROM calibration_checks UNION SELECT sn FROM light_observations UNION SELECT sn FROM sn_models UNION SELECT sn FROM functional_checks UNION SELECT sn FROM soc_observations UNION SELECT sn FROM estop_pairing_checks ORDER BY sn").fetchall()
            return [dict(sn=row["sn"], segment_count=len(self.list_segments(row["sn"])), items=[dict(item=result["item"], status=result["status"], completed=result["completed"]) for result in self.results(row["sn"])]) for row in rows]

    def metrics(self, segment_id):
        """List only metrics actually present in this imported evidence file."""
        with self.lock:
            manifest = self._segment(segment_id)
            units, available = {}, set()
            with _open_evidence(self.packages / manifest["segment_id"] / "segment.sqlite") as database:
                for row in database.execute("SELECT group_name,sequence,observed_at,elapsed_seconds,payload FROM samples ORDER BY group_name,sequence"):
                    payload = _sample(row, manifest)
                    for key, value in payload["values"].items():
                        units[key] = payload.get("units", {}).get(key, metric_spec(key)["unit"])
                        if value is not None:
                            available.add(key)
            return [{**metric_spec(key), "unit": units[key], "available": key in available} for key in sorted(units)]

    def curves(self, segment_id, keys, max_points=800):
        keys = list(dict.fromkeys(_metric.validate_python(key) for key in keys))
        if not 1 <= len(keys) <= 12 or isinstance(max_points, bool) or not isinstance(max_points, int) or not 2 <= max_points <= 2000:
            raise ValueError("请选择1至12个指标，显示点数2至2000")
        with self.lock:
            manifest = self._segment(segment_id)
            path = self.packages / manifest["segment_id"] / "segment.sqlite"
            with _open_evidence(path) as database:
                total = database.execute("SELECT count(*) FROM samples").fetchone()[0]
                width = max(1, math.ceil(total / max_points))
                points, bucket, count, units = [], [], 0, {}
                previous_by_group, previous_stream = {}, None
                for row in database.execute("SELECT group_name,sequence,observed_at,elapsed_seconds,payload FROM samples ORDER BY elapsed_seconds,group_name,sequence"):
                    payload = _sample(row, manifest)
                    units.update(payload.get("units", {}))
                    if not any(key in payload["values"] for key in keys):
                        continue
                    group = row[0]
                    period = 1 / max(1, manifest["sampling_hz"].get(group, 1))
                    gap = (group in previous_by_group and row[3] - previous_by_group[group] > period * 3)
                    gap = gap or (previous_stream is not None and previous_stream != payload.get("stream_id"))
                    payload = {**payload, "_gap_before": bool(gap)}
                    previous_by_group[group], previous_stream = row[3], payload.get("stream_id")
                    count += 1
                    bucket.append((row, payload))
                    if len(bucket) >= width:
                        points.append(_bucket(bucket, keys))
                        bucket = []
                if bucket:
                    points.append(_bucket(bucket, keys))
            return dict(segment_id=manifest["segment_id"], sn=manifest["sn"], source_kind=manifest["source_kind"], state=manifest["state"], time_basis="robot_observed_at_with_original_source_at", count=count, raw_count=total, points=points, metrics=[{**metric_spec(key), "unit": units.get(key, metric_spec(key)["unit"])} for key in keys], missing_counts=manifest["missing_counts"], started_at=manifest["started_at"], ended_at=manifest["ended_at"])

    def export_reports(self, sn, output_dir):
        sn = _sn(sn)
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(output).free < RESERVE_BYTES:
            raise OSError("报告目录磁盘空间不足；已有报告保留")
        with self.lock:
            segments, results = self.list_segments(sn), self.results(sn)
            from .factory_result_summary import motor_parameter_report

            parameters = motor_parameter_report(self, sn)
            if parameters:
                results[0] = {**results[0], "motor_parameters": parameters}
            for index, segment in enumerate(segments):
                try:
                    _verify_file(self.packages / segment["segment_id"] / "segment.sqlite", segment["bytes"], segment["sha256"])
                except (OSError, ValueError) as exc:
                    segment = dict(segment)
                    segment["local_error"] = f"本地原件缺失或校验失败：{exc}"
                    segments[index] = segment
            version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
            # SN remains display text; filenames never contain user-controlled paths.
            prefix = SUITE_CODE + "-SN-" + hashlib.sha256(sn.encode()).hexdigest()[:12] + "-" + version
            destination = output / prefix
            with tempfile.TemporaryDirectory(prefix=".report-", dir=output) as temporary:
                staging = Path(temporary)
                paths = []
                for result in results:
                    item = result["item"]
                    selected = [segment for segment in segments if (5 if segment["test_type"] == "stunt" else 4) == item]
                    for observation in result.get("soc_observations", []):
                        try:
                            identifier = str(UUID(observation["attachment_id"]))
                            attachment = observation["screenshot"]
                            data = _soc_png(self.attachments / identifier)
                            if len(data) != attachment["bytes"] or hashlib.sha256(data).hexdigest() != attachment["sha256"]:
                                raise ValueError("SOC截图原件大小或SHA256不符")
                            name = f'soc-{UUID(observation["observation_id"])}.png'
                            with (staging / name).open("xb") as stream:
                                os.chmod(staging / name, 0o600)
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            observation["screenshot_file"] = name
                        except (OSError, ValueError) as exc:
                            observation["screenshot_error"] = f"本地SOC截图缺失或校验失败：{exc}"
                    csv_path = staging / f"{prefix}-item{item}.csv"
                    with csv_path.open("x", encoding="utf-8-sig", newline="") as stream:
                        writer = csv.writer(stream)
                        writer.writerow(["kind", "sn", "item", "id", "group", "sequence", "observed_at", "elapsed_seconds", "source_at", "source_kind", "values_json", "units_json", "missing_reasons_json", "status", "notes", "event_json", "completed", "board_json", "suite_type", "suite_code", "suite_name", "case_code"])
                        item_case = CALIBRATION_CASE_CODES.get(item, MOTION_CASE_CODES["stunt"] if item == 5 else "")
                        def write_row(values, completed="", board_json="", case_code=""):
                            writer.writerow(_csv_safe([*values, completed, board_json, "whole_robot", SUITE_CODE, SUITE_NAME, case_code]))
                        write_row(["item_state", sn, item, "", "", "", "", "", "", "", "", "", "", result["status"], "28轴不进行特技测试" if result["status"] == "not_applicable" else "独立完成标记不改变人工结论或板卡结果", ""], completed=result["completed"], case_code=item_case)
                        for entry in result["history"]:
                            write_row(["human_result", sn, item, entry["result_id"], "", "", entry["created_at"], "", "", "", "", "", "", entry["status"], entry["notes"], ""], completed=entry.get("completed", entry["status"] != "incomplete"), case_code=item_case)
                        parameter_report = result.get("motor_parameters")
                        if parameter_report:
                            receipt = parameter_report["result"]
                            for sequence, motor in enumerate(receipt["rows"], 1):
                                note = "MIT编码量程核查；固件与CAN_TIMEOUT为读取值，不自动判定合规或整项通过"
                                if parameter_report["source_kind"] == "simulated":
                                    note = "SIMULATED 模拟数据，不能作为实机结果；" + note
                                write_row(["motor_parameter_check", sn, item, parameter_report["request_id"],
                                           motor["address"], sequence, receipt.get("observed_at", ""), "", "",
                                           parameter_report["source_kind"], _json(motor),
                                           _json({"POS": "rad", "SPD": "rad/s", "TOR": "N·m", "can_timeout_ms": "ms"}),
                                           "", motor.get("status", "unknown"), note, ""],
                                          case_code=MOTOR_PARAMETERS_CASE_CODE)
                        pairing = result.get("estop_pairing")
                        if pairing:
                            head = pairing["latest"] or {}
                            write_row(["estop_pairing_state", sn, item, head.get("check_id", ""), pairing["kind"], "", head.get("observed_at", ""), "", "", "operator", "", "", "", pairing["status"], "远程急停开关对频测试；人工结论，未记录时保持未知", _json(pairing)], case_code=ESTOP_PAIRING_CASE_CODE)
                            for entry in pairing["history"]:
                                write_row(["estop_pairing", sn, item, entry["check_id"], entry["kind"], "", entry["observed_at"], "", "", "operator", "", "", "", entry["status"], "现场人工确认", _json(entry)], case_code=ESTOP_PAIRING_CASE_CODE)
                        for light in result["lights"]:
                            write_row(["light_observation", sn, item, "", "灯板", "", light["observed_at"], "", "", "operator", "", "", "", "lit" if light["lit"] else "not_lit", "现场人工观察", ""], case_code=LIGHT_CASE_CODE)
                        for confirmation in result["calibration_confirmations"]:
                            write_row(["calibration_confirmation", sn, item, "", "整机标定", "", confirmation["observed_at"], "", "", "operator", "", "", "", "completed" if confirmation["completed"] else "incomplete", "现场人工确认；逐电机结果未知", ""], case_code=CALIBRATION_CASE_CODES[2])
                        for kind, check in result.get("functional_checks", {}).items():
                            head = check["latest"] or {}
                            write_row(["functional_check_state", sn, item, head.get("check_id", ""), kind, "", head.get("observed_at", ""), "", "", "operator", "", "", "", check["status"], check["name"] + "；人工结论，未记录时保持未完成", _json(check)], case_code=FUNCTION_CASE_CODES[kind])
                            for entry in check["history"]:
                                write_row(["functional_check", sn, item, entry["check_id"], kind, "", entry["observed_at"], "", "", "operator", "", "", "", entry["status"], entry["notes"], _json(entry)], case_code=FUNCTION_CASE_CODES[kind])
                        for sequence, observation in enumerate(result.get("soc_observations", []), 1):
                            values = {key: observation.get(key) for key in ("soc", "previous_soc", "voltage", "current", "uptime_seconds", "soc_age_ms")}
                            note = "SOC变化证据不能独立证明主控不断电、无重启或整项功能通过；截图文件位于本CSV同目录"
                            if observation["source_kind"] == "simulated":
                                note = "SIMULATED 模拟数据；不能作为实机测试结果；" + note
                            if observation.get("screenshot_error"):
                                note += "；" + observation["screenshot_error"]
                            write_row(["soc_observation", sn, item, observation["observation_id"], "battery_hot_swap", sequence, observation["received_at"], observation.get("uptime_seconds"), observation["sampled_at"], observation["source_kind"], _json(values), _json(dict(soc="%", previous_soc="%", voltage="V", current="A", uptime_seconds="s", soc_age_ms="ms")), "", "evidence_only", note, _json(observation)], case_code=FUNCTION_CASE_CODES["battery_hot_swap"])
                        calibration = result.get("calibration")
                        if calibration:
                            for row in calibration["report"]["results"]:
                                write_row(["calibration_check", sn, item, calibration["run_id"], row["joint"], row["index"], calibration.get("saved_at", ""), "", "", "hardware", _json(row), "", "", row["status"], "单侧检查；跳过或未测不算合格", ""], case_code=CALIBRATION_CASE_CODES[3])
                        board = result["board"]
                        if board:
                            for check in board["checks"]:
                                head = check["latest"] or {}
                                write_row(["board_check", sn, item, head.get("report_id", ""), check["id"], "", head.get("created_at", ""), "", "", board["source_kind"], "", "", "", head.get("result", "unchecked"), head.get("message", "未检查，结果未知"), ""], board_json=_json({**check, "robot_id": board["robot_id"], "source_kind": board["source_kind"]}), case_code=BOARD_CASE_CODES[check["id"]])
                            for entry in board["history"]:
                                write_row(["board_history", sn, item, entry["report_id"], entry["id"], "", entry["created_at"], "", "", entry["source_kind"], "", "", "", entry["result"], entry["message"], ""], board_json=_json(entry), case_code=BOARD_CASE_CODES[entry["id"]])
                        for segment in selected:
                            motion_case = MOTION_CASE_CODES[segment["test_type"]]
                            if segment.get("local_error"):
                                write_row(["unavailable", sn, item, segment["segment_id"], "", "", "", "", "", segment["source_kind"], "", "", "", "missing_evidence", segment["local_error"], ""], case_code=motion_case)
                                continue
                            summary = self.segment_summary(segment["segment_id"])
                            write_row(["key_data_summary", sn, item, segment["segment_id"], "summary", "", segment["ended_at"], summary["duration_seconds"], "", segment["source_kind"], _json(summary), "", "", "unverified", "记录时长要求超过2小时；不等于动作时长或性能合格", ""], case_code=motion_case)
                            with _open_evidence(self.packages / segment["segment_id"] / "segment.sqlite") as database:
                                for row in database.execute("SELECT group_name,sequence,observed_at,elapsed_seconds,payload FROM samples ORDER BY elapsed_seconds,group_name,sequence"):
                                    payload = _sample(row, segment)
                                    write_row(["sample", sn, item, segment["segment_id"], row[0], row[1], row[2], row[3], payload.get("source_at"), segment["source_kind"], _json(payload["values"]), _json(payload.get("units", {})), _json(payload.get("missing_reasons", {})), "", "", ""], case_code=motion_case)
                                for sequence, observed_at, raw in database.execute("SELECT sequence,observed_at,payload FROM events ORDER BY sequence"):
                                    event = json.loads(raw)
                                    publisher = event.get("source_at")
                                    write_row(["event", sn, item, segment["segment_id"], "events", sequence, observed_at, event.get("elapsed_seconds"), _json(publisher) if publisher is not None else "", segment["source_kind"], "", "", "", event.get("event_type", event.get("type", "")), str(event.get("message", "")), _json(event)], case_code=motion_case)
                        stream.flush()
                        os.fsync(stream.fileno())
                    report_path = staging / f"{prefix}-item{item}.html"
                    with report_path.open("x", encoding="utf-8") as stream:
                        stream.write(self._report_html(sn, result, selected, csv_path.name, version))
                        stream.flush()
                        os.fsync(stream.fileno())
                    paths.extend([str(report_path), str(csv_path)])
                _sync_directory(staging)
                os.rename(staging, destination)
                _sync_directory(output)
                return [str(destination / Path(path).name) for path in paths]

    def _report_html(self, sn, result, segments, csv_name, version):
        def esc(value):
            return html.escape(str(value), quote=True)
        board = result["board"]
        simulated = (any(segment["source_kind"] == "simulated" for segment in segments)
                     or bool(board and board["source_kind"] == "simulated")
                     or any(row["source_kind"] == "simulated" for row in result.get("soc_observations", []))
                     or result.get("motor_parameters", {}).get("source_kind") == "simulated")
        warning = '<p class="warning">SIMULATED 模拟数据 · 不能作为实机测试结果</p>' if simulated else ""
        conclusion_label = "第4项有效结果" if result.get("functional_checks") else "人工结论"
        parts = [f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="suite_type" content="whole_robot"><meta name="suite_code" content="{esc(SUITE_CODE)}"><meta name="suite_name" content="{esc(SUITE_NAME)}"><title>{esc(sn)} · {esc(result["name"])}</title><style>body{{font:14px sans-serif;max-width:1100px;margin:30px auto;color:#182438;padding:0 16px}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #bcc6d2;padding:6px;text-align:left;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.warning{{font-weight:bold;color:#ad2134;border:2px solid;padding:12px}}svg{{max-width:100%;height:auto}}section{{break-inside:avoid}}@media print{{button{{display:none}}body{{margin:0}}}}</style><body><button onclick="window.print()">打印 / 保存PDF</button><h1>{esc(SUITE_NAME)} {esc(SUITE_CODE)} · 第{result["item"]}项：{esc(result["name"])}</h1><p>SN：<strong>{esc(sn)}</strong> · 报告版本：{esc(version)}</p>{warning}<p>{conclusion_label}：<strong>{esc(STATUS_NAMES[result["status"]])}</strong>。人工填写与观测数据分别保留，不代表自动验收、审批或MTBF结果。</p><p>缺失只提示，不阻止后续测试或报告。策略名来自操作员标签，策略目标不等于最终电机目标。<a href="{esc(csv_name)}">完整数据CSV</a></p>']
        if result["status"] == "not_applicable":
            parts.append('<p>本SN机型为28轴，第5项特技测试不适用，无需执行，不计为通过或待完成；既有历史记录保留。</p>')
        else:
            parts.append(f'<p>独立完成标记：<strong>{"已完成" if result["completed"] else "未完成"}</strong>。完成标记与人工结论、逐项板卡结果分别保存，勾选完成不代表测试通过。</p>')
        parameter_report = result.get("motor_parameters")
        if parameter_report:
            from .factory_result_summary import _display, _range

            receipt = parameter_report["result"]
            parts.append(f'<section><h2>{MOTOR_PARAMETERS_CASE_CODE} · 电机参数检查明细</h2><p>机器人：{esc(parameter_report["robot_id"])} · 覆盖 {len(receipt["rows"])}/{parameter_report["dof"]} 台 · 读取时间：{esc(receipt.get("observed_at", "未知"))} · 附件SHA256：{esc(parameter_report["sha256"])}</p><p>各量程列为“实际 / 期望”。固件和CAN_TIMEOUT仅记录读取值；量程一致不自动代表整项通过。</p><table><tr><th>电机 / 型号</th><th>固件（读取值）</th><th>KP</th><th>KD</th><th>POS / rad</th><th>SPD / rad/s</th><th>TOR / N·m</th><th>CAN_TIMEOUT / ms</th><th>量程检查结果</th></tr>')
            for motor in receipt["rows"]:
                cells = [f'{motor["address"]} · 型号 {_display(motor.get("model"))}',
                         _display(motor.get("firmware"))]
                for key in ("KP", "KD", "POS", "SPD", "TOR"):
                    value = motor["parameters"].get(key)
                    cells.append(f'{_range(value.get("actual"))} / {_range(value.get("expected"))}'
                                 if value else "未读取")
                cells += [_display(motor.get("can_timeout_ms")),
                          {"match": "范围一致", "different": "有差异", "unreadable": "未完成"}.get(motor.get("status"), "未知")]
                parts.append('<tr>' + ''.join(f'<td>{esc(cell)}</td>' for cell in cells) + '</tr>')
            parts.append('</table></section>')
        if result.get("functional_checks"):
            parts.append('<p>现场判据：远程急停使用独立急停遥控器／无线急停按钮切断电机电源，主控保持供电，核对电机断电、主控持续运行、保持及解除状态。电机反馈中断不等于主控掉电，也不能单独证明电机已断电。热换电允许全程连接网线，要求主控全程不断电、无重启，并核对通信及运行状态。未观测的现象不能据遥测恢复推断为通过。</p>')
            parts.append('<section><h2>整机功能测试：远程急停与热换电</h2><p>24轴与28轴均适用。人员在机器人现场操作并填写实际观察；上位机不触发急停、换电或策略。子项不要求持续2小时；动作记录仍要求每段超过2小时。数据中断不能自动证明急停或换电成功，也不推断断电原因。</p><p>第4项有效结果同时考虑整项人工结论和以下两项最新人工结论；任一子项失败时第4项不通过；缺失或未完成时不能标记完成。两个子项均已明确填写通过或失败后，可标记测试已完成，已完成不代表通过。历史原始结论保留。</p><table><tr><th>功能</th><th>最新人工结论</th><th>记录时间</th><th>现场观察 / 备注</th><th>关联片段 / SOC证据</th></tr>')
            for check in result["functional_checks"].values():
                head = check["latest"] or {}
                associated = "片段：" + (", ".join(head.get("segment_ids", [])) or "无") + "；SOC：" + (", ".join(head.get("soc_observation_ids", [])) or "无")
                parts.append(f'<tr><td>{esc(FUNCTION_CASE_CODES[check["kind"]])} · {esc(check["name"])}</td><td>{esc(STATUS_NAMES[check["status"]])}</td><td>{esc(head.get("observed_at", "未记录"))}</td><td><pre>{esc(head.get("notes", ""))}</pre></td><td>{esc(associated)}</td></tr>')
            parts.append('</table><h3>功能测试复测历史（人工结论）</h3><table><tr><th>功能</th><th>记录时间</th><th>人工结论</th><th>现场观察 / 备注</th><th>关联片段 / SOC证据</th></tr>')
            for check in result["functional_checks"].values():
                for entry in check["history"]:
                    associated = "片段：" + (", ".join(entry["segment_ids"]) or "无") + "；SOC：" + (", ".join(entry.get("soc_observation_ids", [])) or "无")
                    parts.append(f'<tr><td>{esc(FUNCTION_CASE_CODES[check["kind"]])} · {esc(check["name"])}</td><td>{esc(entry["observed_at"])}</td><td>{esc(STATUS_NAMES[entry["status"]])}</td><td><pre>{esc(entry["notes"])}</pre></td><td>{esc(associated)}</td></tr>')
            parts.append('</table></section>')
        if result.get("soc_observations"):
            parts.append('<section><h2>开始换电后的SOC观察与截图历史</h2><p>SOC变化证据不能独立证明主控不断电、无重启或整项功能通过。主控供电、重启及通信运行状态仍由人员按原测试条件填写结论；缺失数值保持未知。以下按电脑接收保存顺序排列，保留原采样时间和接收时间。</p><table><tr><th>观察编号 / 会话</th><th>来源 / 机器人</th><th>事件 / SOC</th><th>电压 / 电流 / 主控运行秒数</th><th>原采样时间 / 接收时间 / 新鲜度</th><th>连续性观察</th><th>PNG截图</th></tr>')
            for observation in result["soc_observations"]:
                source = "SIMULATED 模拟数据，不能作为实机测试结果" if observation["source_kind"] == "simulated" else "ros2（不等于已完成实机验收）"
                screenshot = observation["screenshot"]
                image = f'<p class="warning">{esc(observation["screenshot_error"])}</p>' if observation.get("screenshot_error") else "截图未导出"
                if observation.get("screenshot_file"):
                    image = f'<a href="{esc(observation["screenshot_file"])}"><img src="{esc(observation["screenshot_file"])}" alt="SOC观察截图" style="max-width:240px;height:auto"></a>'
                image += f'<p>附件 {esc(observation["attachment_id"])} · SHA256 {esc(screenshot["sha256"])} · {esc(screenshot["bytes"])} 字节</p>'
                values = {key: "未知" if observation.get(key) is None else observation[key]
                          for key in ("voltage", "current", "uptime_seconds", "previous_soc")}
                readings = f'{values["voltage"]} V / {values["current"]} A / {values["uptime_seconds"]} s'
                parts.append(f'<tr><td>{esc(FUNCTION_CASE_CODES["battery_hot_swap"])}<br>{esc(observation["observation_id"])}<br>{esc(observation["session_id"])}</td><td>{esc(source)}<br>{esc(observation["robot_id"])}</td><td>{esc(observation["event"])}<br>{esc(observation["soc"])} % · 前值 {esc(values["previous_soc"])} %</td><td>{esc(readings)}</td><td>{esc(observation["sampled_at"])}<br>{esc(observation["received_at"])}<br>{esc(observation["soc_age_ms"])} ms</td><td>{esc(observation["continuity"])}</td><td>{image}</td></tr>')
            parts.append('</table></section>')
        if not result["history"] and result["status"] != "not_applicable":
            parts.append("<p>尚未填写人工结论，完成标记独立显示；已同步的板卡结果见下表。</p>")
        for entry in result["history"]:
            completed = entry.get("completed", entry["status"] != "incomplete")
            parts.append(f'<section><h2>人工结果 / 复测记录</h2><p>{esc(entry["created_at"])} · {esc(STATUS_NAMES[entry["status"]])} · 独立完成标记：{"已完成" if completed else "未完成"}</p><pre>{esc(entry["notes"])}</pre><p>关联片段：{esc(", ".join(entry["segment_ids"]) or "无")}</p></section>')
            for identifier in entry.get("attachment_ids", []):
                row = self.db.execute("SELECT payload FROM attachments WHERE attachment_id=?", (identifier,)).fetchone()
                attachment = json.loads(row["payload"])
                parts.append(f'<p>附件：{esc(attachment["name"])} · SHA256 {esc(attachment["sha256"])} · {attachment["bytes"]} 字节（保存在电脑本地证据目录）</p>')
        pairing = result.get("estop_pairing")
        if pairing:
            head = pairing["latest"] or {}
            parts.append(f'<section><h2>{esc(ESTOP_PAIRING_CASE_CODE)} · 远程急停开关对频测试</h2><p>人员在现场操作并点击通过／不通过；上位机仅保存人工结论，不发送对频或急停命令。该结论独立保存，不自动改变第1项或第4项结果。</p><p>最新人工结论：<strong>{esc(STATUS_NAMES[pairing["status"]])}</strong> · 记录时间：{esc(head.get("observed_at", "未记录"))}</p><h3>对频测试完整复测历史（人工结论）</h3><table><tr><th>记录编号</th><th>记录时间</th><th>人工结论</th></tr>')
            for entry in pairing["history"]:
                parts.append(f'<tr><td>{esc(entry["check_id"])}</td><td>{esc(entry["observed_at"])}</td><td>{esc(STATUS_NAMES[entry["status"]])}</td></tr>')
            parts.append('</table></section>')
        if result["lights"]:
            parts.append('<section><h2>灯板现场观察</h2><p>以下是人员观察，未自动控制灯板；复测历史保留。</p><table><tr><th>记录时间</th><th>人工确认</th></tr>')
            for light in result["lights"]:
                parts.append(f'<tr><td>{esc(LIGHT_CASE_CODE)} · {esc(light["observed_at"])}</td><td>{"亮了" if light["lit"] else "没亮"}</td></tr>')
            parts.append('</table></section>')
        if result["calibration_confirmations"]:
            parts.append('<section><h2>整机标定现场确认</h2><p>由人员确认流程状态；没有逐电机结果，不能据此判定写零通过。</p><table><tr><th>记录时间</th><th>人工确认</th></tr>')
            for confirmation in result["calibration_confirmations"]:
                parts.append(f'<tr><td>{esc(CALIBRATION_CASE_CODES[2])} · {esc(confirmation["observed_at"])}</td><td>{"已完成" if confirmation["completed"] else "中断 / 未完成"}</td></tr>')
            parts.append('</table></section>')
        calibration = result.get("calibration")
        if calibration:
            parts.append(f'<section><h2>标定检查逐关节结果</h2><p>机器人：{esc(calibration["robot_id"])} · 轮次：{esc(calibration["run_id"])} · 原始保存时间：{esc(calibration.get("saved_at", "未知"))}。跳过、未测或中断不算合格；人工结论独立保存。</p><table><tr><th>关节</th><th>方向</th><th>参考角度</th><th>结果</th><th>原始尝试</th></tr>')
            for row in calibration["report"]["results"]:
                parts.append(f'<tr><td>{esc(CALIBRATION_CASE_CODES[3])} · {esc(row["joint"])}</td><td>{esc(row["direction"])}</td><td>{esc(row["reference_deg"])}</td><td>{esc(row["status"])}</td><td><pre>{esc(_json(row["attempts"]))}</pre></td></tr>')
            parts.append('</table></section>')
        if board:
            counts = board["counts"]
            unfinished = counts["error"] + counts["needs_review"] + counts["skip"]
            assessment = board.get("assessment") or {}
            total = board.get("display_counts") or assessment.get("counts") or {"通过": counts["pass"], "不通过": counts["fail"], "未完成": unfinished, "未检查": counts["unchecked"]}
            parts.append(f'<section><h2>{esc(BOARD_CASE_CODES["RP001"])} · 基础自检（17项子检查）</h2><p>机器人：{esc(board["robot_id"])} · 来源：{esc(board["source_kind"])} · 已有结果 {board["executed"]} / {board["total"]} 项。通过 {total["通过"]}，不通过 {total["不通过"]}，未完成 {total["未完成"]}，未检查 {total["未检查"]}。</p><p>17项统一归入一个整机测试编号，逐项结果与复测历史独立保留。逐项结果不代替整个第1项结论。未完成项显示原因和处理建议；原始证据在下方展开查看。</p>')
            if assessment:
                parts.append(f'<p>本次标准：{esc(assessment["profile"]["name"])} · 判定时间：{esc(assessment["assessed_at"])}</p>')
            parts.append('<table><tr><th>检查项</th><th>测试结果</th><th>结果说明 / 处理建议</th><th>原始完成时间</th></tr>')
            for check in board["checks"]:
                head = check["latest"] or {}
                saved_view = assessment.get("checks", {}).get(check["id"])
                view = BoardCheckView(**saved_view) if saved_view else board_check_view(check["id"], head)
                explanation = "\n".join((view.summary, *view.readings, f"处理建议：{view.action}" if view.action else ""))
                parts.append(f'<tr><td>{esc(BOARD_CASE_CODES[check["id"]])} · {esc(check["id"])} · {esc(check["label"])}</td><td>{esc(view.label)}</td><td><pre>{esc(explanation)}</pre></td><td>{esc(head.get("created_at", "未提供"))}</td></tr>')
            parts.append("</table><details><summary>展开原始证据与复测历史（排查用）</summary><p>以下为逐项状态快照，并非完整原始执行报告。按本机接收顺序保留，原始结论不改写；机器人时钟回拨不会隐藏后收到的失败。</p>")
            if assessment:
                parts.append(f'<h4>本次冻结的标准与判定依据</h4><pre>{esc(_json(assessment))}</pre>')
            for entry in board["history"]:
                parts.append(f'<h4>{esc(BOARD_CASE_CODES[entry["id"]])} · {esc(entry["id"])} · {esc(entry["result"])} · {esc(entry["created_at"])}</h4><pre>{esc(_json(entry))}</pre>')
            parts.append("</details></section>")
        if not segments and result["status"] != "not_applicable":
            parts.append("<p>本项暂无导入运动日志。缺失数据保持未知；仍可填写备注、继续测试并出报告。</p>")
        for segment in segments:
            identifier = segment["segment_id"]
            motion_case = MOTION_CASE_CODES[segment["test_type"]]
            if segment.get("local_error"):
                parts.append(f'<section><h2>{esc(motion_case)} · 片段 {esc(identifier)}</h2><p class="warning">{esc(segment["local_error"])}</p><p>原始来源 {esc(segment["source_kind"])}，原始起止 {esc(segment["started_at"])} 至 {esc(segment["ended_at"])}。未读取或编造该段曲线和统计；人工结果保持原样。</p></section>')
                continue
            catalog = self.metrics(identifier)
            summary = self.segment_summary(identifier)
            duration = summary["duration_seconds"]
            duration_text = "未知" if duration is None else f"{duration / 60:.2f}分钟"
            duration_status = "超过2小时" if summary["duration_satisfied"] else "时长不足或未知"
            parts.append(f'<p><strong>记录时长：{esc(duration_text)}，{esc(duration_status)}</strong>。记录时长不等于动作时长，不构成自动性能验收。</p>')
            if summary.get("metrics"):
                parts.append('<p>关键数据模式：1Hz趋势；统计包含10Hz运动数据观察。观察次数不等于独立反馈帧数。缺失保持未知。</p><table><tr><th>指标 / 单位</th><th>最小值</th><th>最大值</th><th>均值</th><th>有效 / 观察次数</th></tr>')
                for key, value in sorted(summary["metrics"].items()):
                    parts.append(f'<tr><td>{esc(key)} / {esc(value["unit"])}</td><td>{esc(value["minimum"])}</td><td>{esc(value["maximum"])}</td><td>{esc(value["mean"])}</td><td>{value["valid"]} / {value["observations"]}</td></tr>')
                parts.append(f'</table><p>异常观察{esc(summary.get("exception_count", 0))}次；保存最多3组前后关键快照，其余异常次数仍保留。完整统计与快照见CSV的key_data_summary。</p>')
            shown_keys = [entry["key"] for entry in sorted(catalog, key=lambda entry: (not entry["available"], 0 if entry["key"].startswith("battery.") else 1 if "temperature" in entry["key"] else 2, entry["key"]))[:12]]
            parts.append(f'<section><h2>{esc(motion_case)} · {esc(segment["test_type"])} · {esc(segment["policy"] or "策略标签未填写")}</h2><p>片段 {esc(identifier)} · 采集身份 {esc(segment["robot_id"])} · 来源 {esc(segment["source_kind"])} · 状态 {esc(segment["state"])}</p><p>原始起止：{esc(segment["started_at"])} 至 {esc(segment["ended_at"])}；电脑导入时间：{esc(segment["imported_at"])}</p><p>采样数：{esc(_json(segment["sample_counts"]))}；缺失计数：{esc(_json(segment["missing_counts"]))}；日志SHA256：{esc(segment["sha256"])}</p><p>异常：{esc(segment["fault"] or "未提供异常信息，不等于确认无异常")}</p>')
            parts.append("<h3>策略 / 运行事件时间线</h3><p>仅记录来源已发布且采集端实际收到的事件；来源未提供时不推断策略切换、动作发生或完成。观察时间来自机器人采集端，原publisher时间保留原值，不能假定两者精确同步。</p>")
            with _open_evidence(self.packages / identifier / "segment.sqlite") as database:
                events = database.execute("SELECT sequence,observed_at,payload FROM events ORDER BY sequence LIMIT 500").fetchall()
            if not events:
                parts.append("<p>本段未收到事件；不能据此认定没有发生策略切换或异常。</p>")
            else:
                parts.append("<table><tr><th>序号 / 观察时间</th><th>原publisher时间</th><th>经过秒数</th><th>来源 / 节点 / 类型或级别</th><th>日志原文</th></tr>")
                for sequence, observed_at, raw in events:
                    event = json.loads(raw)
                    source_time = _json(event["source_at"]) if event.get("source_at") is not None else "未提供"
                    event_type = event.get("event_type", event.get("type", "类型未提供"))
                    origin = f'{event.get("source", "来源未提供")} / {event.get("node", "节点未提供")} / {event_type} / {event.get("level", "级别未提供")}'
                    parts.append(f'<tr><td>{sequence} / {esc(observed_at)}</td><td>{esc(source_time)}</td><td>{esc(event.get("elapsed_seconds", "未提供"))}</td><td>{esc(origin)}</td><td><pre>{esc(event.get("message", "原文未提供"))}</pre></td></tr>')
                parts.append("</table>")
                if segment["event_count"] > len(events):
                    parts.append(f'<p>HTML显示前{len(events)}条事件；全部{segment["event_count"]}条及完整JSON见本项CSV的event行。</p>')
            if shown_keys:
                data = self.curves(identifier, shown_keys, max_points=300)
                parts.append("<p>下图横轴为机器人记录的单调经过秒数；保留原始UTC/带时区时间及来源时间于CSV。缺失不插值。每桶显示均值与极值；含缺失桶不连线。最多展示12项，全部已保存指标见CSV。</p>")
                parts.append("<table><tr><th>指标 / 单位</th><th>最小值</th><th>最大值</th><th>有效 / 已记录</th></tr>")
                for metric in data["metrics"]:
                    values = [point["values"][metric["key"]] for point in data["points"]]
                    low = [value["minimum"] for value in values if value["minimum"] is not None]
                    high = [value["maximum"] for value in values if value["maximum"] is not None]
                    parts.append(f'<tr><td>{esc(metric["label"])} / {esc(metric["unit"])}</td><td>{esc(min(low) if low else "未知")}</td><td>{esc(max(high) if high else "未知")}</td><td>{sum(value["valid"] for value in values)} / {sum(value["total"] for value in values)}</td></tr>')
                parts.append("</table><p>ROS 2来源不等于已完成实机验收；模拟数据始终保持模拟标记。</p>")
                for metric in data["metrics"]:
                    parts.append(_svg(data["points"], metric))
            else:
                parts.append("<p>本段没有采样数据，曲线不可用。零数据不是零测量值，也不自动决定人工结果。</p>")
            parts.append("</section>")
        return "".join(parts) + "</body></html>"


def _csv_safe(values):
    # Spreadsheet programs must not interpret operator text as formulas.
    return ["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values]


def _bucket(rows, keys):
    row, payload = rows[-1]
    values = {}
    for key in keys:
        relevant = [sample["values"][key] for _, sample in rows if key in sample["values"]]
        valid = [value for value in relevant if value is not None]
        values[key] = dict(value=sum(valid) / len(valid) if valid else None, minimum=min(valid) if valid else None, maximum=max(valid) if valid else None, valid=len(valid), total=len(relevant))
    return dict(time=row[2], elapsed_seconds=row[3], source_at=payload.get("source_at"), stream_id=payload.get("stream_id"), group_name=row[0], sequence=row[1], gap_before=any(sample.get("_gap_before", False) for _, sample in rows), values=values)


def _svg(points, metric):
    def esc(value):
        return html.escape(str(value), quote=True)
    key = metric["key"]
    valid = [point for point in points if point["values"][key]["value"] is not None]
    title = f'{metric["label"]} / {metric["unit"]}'
    if not valid:
        return f"<p>{esc(title)}：无有效数据。</p>"
    low = min(point["values"][key]["minimum"] for point in valid)
    high = max(point["values"][key]["maximum"] for point in valid)
    if low == high:
        low, high = low - 1, high + 1
    start, end = points[0]["elapsed_seconds"], points[-1]["elapsed_seconds"]
    span = max(end - start, 0.001)
    def x(elapsed):
        return 65 + 700 * (elapsed - start) / span

    def y(value):
        return 165 - 125 * (value - low) / (high - low)
    paths, current, extrema, previous_stream = [], [], [], None
    for point in points:
        value = point["values"][key]
        good = value["value"] is not None and value["valid"] == value["total"]
        if not good or point.get("gap_before") or (previous_stream is not None and previous_stream != point["stream_id"]):
            if current:
                paths.append(" ".join(current))
                current = []
        if good:
            current.append(f'{x(point["elapsed_seconds"]):.2f},{y(value["value"]):.2f}')
        if value["minimum"] is not None:
            xpos = x(point["elapsed_seconds"])
            extrema.append(f'<path d="M{xpos:.2f},{y(value["minimum"]):.2f} V{y(value["maximum"]):.2f}" stroke="#9aaac3"/>')
        previous_stream = point["stream_id"]
    if current:
        paths.append(" ".join(current))
    lines = "".join(f'<polyline points="{path}" fill="none" stroke="#2859a8" stroke-width="1.5"/>' for path in paths)
    dots = "".join(f'<circle cx="{path.split()[0].split(",")[0]}" cy="{path.split()[0].split(",")[1]}" r="2" fill="#2859a8"/>' for path in paths if len(path.split()) == 1)
    return f'<svg viewBox="0 0 800 210" role="img" aria-label="{esc(title)}"><text x="65" y="20">{esc(title)}</text><path d="M65,40 V165 H765" fill="none" stroke="#8695a8"/><text x="5" y="46">{high:.4g}</text><text x="5" y="165">{low:.4g}</text><text x="65" y="190">{start:.3f}s</text><text x="690" y="190">{end:.3f}s</text>{"".join(extrema)}{lines}{dots}</svg>'
