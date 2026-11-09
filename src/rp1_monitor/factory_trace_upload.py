"""Bounded frozen sidecars for explicit factory submissions, never live capture."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import UUID

from .factory_bridge import BridgeError, private_file

MAX_TRACE_BYTES = 8 * 1024**2


# rp1.summary-trace.v1 uses the unchanged 17-column wire projection.
TRACE_FIELDS = ("record_id", "station_id", "sample_id", "model", "suite", "category", "level",
                "case_code", "source_id", "test_name", "started_at", "ended_at", "operator",
                "actual_result", "criterion", "verdict", "notes")


def validate_trace(raw, csv_raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_TRACE_BYTES:
        raise BridgeError("invalid_factory_trace_size")

    def bad():
        raise BridgeError("invalid_factory_trace")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                bad()
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=lambda _: bad())
        rows = list(csv.reader(io.StringIO(csv_raw.decode("utf-8-sig"), newline="")))[1:]
        if (not isinstance(value, dict) or value.get("schema") != "rp1.summary-trace.v1"
                or value.get("csv_sha256") != hashlib.sha256(csv_raw).hexdigest()
                or value.get("formal_statistics_eligible") is not False
                or not rows or any(len(r) != len(TRACE_FIELDS) for r in rows)
                or {r[2] for r in rows} != {value.get("sample_id")}):
            raise BridgeError("factory_trace_csv_mismatch")
        for name in ("rows", "edit_history", "source_history"):
            if not isinstance(value.get(name), list) or any(not isinstance(r, dict) for r in value[name]):
                bad()
        for name in ("identity_scope", "history_scope"):
            if not isinstance(value.get(name), str) or not value[name].strip():
                bad()
        for edit in value["edit_history"]:
            if (edit.get("sn") != value["sample_id"]
                    or any(not isinstance(edit.get(k), str) or not edit[k].strip() for k in ("row_key", "source_token"))
                    or not isinstance(edit.get("before"), dict) or not isinstance(edit.get("after"), dict)):
                bad()
        expected = {r[0]: r for r in rows}
        if len(expected) != len(rows):
            bad()
        seen = set()
        for row in value["rows"]:
            if any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("row_key", "source_token")):
                bad()
            record_id = row.get("record_id")
            if not isinstance(record_id, str) or record_id in seen or record_id not in expected:
                bad()
            seen.add(record_id)
            cells = []
            for field in TRACE_FIELDS:
                cell = row.get(field)
                if not isinstance(cell, str):
                    bad()
                if field == "notes" and row.get("source_kind") == "simulated":
                    cell = "\n".join(filter(None, (cell, "数据来源：simulated（模拟数据，不代表实机验收）")))
                probe = cell.lstrip(" \t\r\n\v\f\ufeff")
                if probe.startswith(("=", "+", "-", "@")) or cell.startswith(("\t", "\r", "\n")):
                    cell = "'" + cell
                cells.append(cell)
            if cells != expected[record_id]:
                raise BridgeError("factory_trace_csv_mismatch")
        if seen != set(expected):
            bad()
    except (ValueError, TypeError, AttributeError, IndexError, UnicodeError, RecursionError, csv.Error):
        raise BridgeError("invalid_factory_trace") from None
    return hashlib.sha256(raw).hexdigest()


def read_trace(csv_path, csv_raw):
    path = Path(str(csv_path) + ".trace.json")
    # A broken symlink is not a missing optional legacy sidecar.
    if not os.path.lexists(path):
        return None
    raw = private_file(path, MAX_TRACE_BYTES)
    validate_trace(raw, csv_raw)
    return raw


def validate_trace_receipt(archive_id, digest, receipt):
    try:
        valid_id = str(UUID(receipt["id"])) == str(UUID(archive_id))
    except (ValueError, TypeError, KeyError, AttributeError):
        valid_id = False
    if (not valid_id or receipt.get("trace_sha256") != digest
            or receipt.get("formal_statistics_eligible") is not False):
        raise BridgeError("factory_trace_receipt_mismatch")
    return {"id": archive_id, "trace_sha256": digest, "formal_statistics_eligible": False}
