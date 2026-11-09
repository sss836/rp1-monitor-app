"""Mock-only two-stage factory evidence delivery; no real platform or devices."""

import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_factory_bridge import archive_receipt, csv_file, platform, upload

from rp1_monitor.factory_bridge import BridgeError, Outbox
from rp1_monitor.factory_trace_upload import TRACE_FIELDS, read_trace, validate_trace


def trace_bytes(raw, **changes):
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))[1:]
    return json.dumps({"schema": "rp1.summary-trace.v1", "sample_id": rows[0][2],
                       "csv_sha256": hashlib.sha256(raw).hexdigest(),
                       "rows": [{**dict(zip(TRACE_FIELDS, r)), "row_key": r[0], "source_token": "synthetic"} for r in rows],
                       "edit_history": [], "source_history": [],
                       "identity_scope": "local OS account only", "history_scope": "since upgrade",
                       "formal_statistics_eligible": False, **changes}).encode()


def trace_receipt(metadata, trace):
    return {"id": archive_receipt(metadata)["id"], "trace_sha256": hashlib.sha256(trace).hexdigest(),
            "formal_statistics_eligible": False}


class TraceUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = csv_file(self.temp.name)
        self.metadata, self.raw = upload(self.path)
        self.trace = trace_bytes(self.raw)
        self.state = Path(self.temp.name) / "queue" / "outbox.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_legacy_sidecar_allowed_but_mismatch_and_symlink_rejected(self):
        self.assertIsNone(read_trace(self.path, self.raw))
        sidecar = Path(str(self.path) + ".trace.json")
        for change in ({"sample_id": "other"}, {"csv_sha256": "0" * 64}, {"rows": []},
                       {"formal_statistics_eligible": True}):
            sidecar.write_bytes(trace_bytes(self.raw, **change))
            with self.assertRaises(BridgeError):
                read_trace(self.path, self.raw)
        sidecar.unlink()
        sidecar.symlink_to(Path(self.temp.name) / "missing")
        with self.assertRaises(BridgeError):
            read_trace(self.path, self.raw)

    def test_restart_after_lost_trace_receipt_never_reposts_csv(self):
        client, transport = platform([(200, {"data": archive_receipt(self.metadata)}, []),
                                      (503, {}, []),
                                      (200, {"data": trace_receipt(self.metadata, self.trace)}, [])])
        box = Outbox(self.state)
        key = box.enqueue(client.base_url, "owner-1", self.metadata, self.raw, trace_raw=self.trace)
        with self.assertRaises(BridgeError):
            box.drain(client, task_key=key)
        saved = box.db.execute("SELECT receipt,payload,trace_payload,status FROM tasks").fetchone()
        self.assertIsNotNone(saved[0])
        self.assertEqual(saved[1:], (self.raw, self.trace, "pending"))
        box.close()
        self.path.write_text("changed original, never reread during retry")
        box = Outbox(self.state)
        try:
            self.assertEqual(box.enqueue(client.base_url, "owner-1", self.metadata, self.raw, trace_raw=self.trace), key)
            box.drain(client, task_key=key)
            requests = transport.requests[1:]
            self.assertEqual(len([r for r in requests if "/robot-result-archives?" in r.full_url]), 1)
            self.assertEqual([r.data for r in requests[1:]], [self.trace, self.trace])
            self.assertEqual(requests[1].get_header("Idempotency-key"), requests[2].get_header("Idempotency-key"))
            query = parse_qs(urlsplit(requests[0].full_url).query)
            self.assertEqual(query["trace_sha256"], [hashlib.sha256(self.trace).hexdigest()])
            self.assertEqual(box.db.execute("SELECT status,payload,trace_payload,bytes FROM tasks").fetchone(), ("sent", None, None, 0))
            self.assertIsNotNone(box.db.execute("SELECT trace_receipt FROM tasks").fetchone()[0])
        finally:
            box.close()

    def test_wrong_trace_receipt_does_not_clear_evidence_or_confirm_delivery(self):
        for change in ({"id": "00000000-0000-4000-8000-000000000099"},
                       {"trace_sha256": "0" * 64}, {"formal_statistics_eligible": True}):
            with self.subTest(change=change):
                client, _ = platform([(200, {"data": archive_receipt(self.metadata)}, []),
                                      (200, {"data": {**trace_receipt(self.metadata, self.trace), **change}}, [])])
                box = Outbox(self.state)
                try:
                    key = box.enqueue(client.base_url, "owner-1", {**self.metadata, "reason": str(change)}, self.raw, trace_raw=self.trace)
                    with self.assertRaisesRegex(BridgeError, "factory_trace_receipt_mismatch"):
                        box.drain(client, task_key=key)
                    self.assertEqual(box.db.execute("SELECT status,trace_payload FROM tasks WHERE task_key=?", (key,)).fetchone(), ("pending", self.trace))
                finally:
                    box.close()

    def test_permanent_trace_conflict_keeps_csv_receipt_and_does_not_block_next_task(self):
        client, _ = platform([(200, {"data": archive_receipt(self.metadata)}, []),
                              (409, {"error": {"code": "factory_trace_conflict"}}, []),
                              (200, {"data": archive_receipt(self.metadata)}, [])])
        box = Outbox(self.state)
        try:
            bad = box.enqueue(client.base_url, "owner-1", self.metadata, self.raw, trace_raw=self.trace)
            good = box.enqueue(client.base_url, "owner-1", {**self.metadata, "reason": "next"}, self.raw)
            self.assertEqual([r["task_key"] for r in box.drain(client)], [good])
            row = box.db.execute("SELECT status,receipt,trace_payload FROM tasks WHERE task_key=?", (bad,)).fetchone()
            self.assertEqual(row[0], "blocked")
            self.assertIsNotNone(row[1])
            self.assertEqual(row[2], self.trace)
        finally:
            box.close()

    def test_legacy_csv_downgrade_rejected_by_server_is_isolated(self):
        client, _ = platform([(409, {"error": {"code": "factory_trace_required"}}, [])])
        box = Outbox(self.state)
        try:
            key = box.enqueue(client.base_url, "owner-1", self.metadata, self.raw)
            with self.assertRaisesRegex(BridgeError, "factory_trace_required"):
                box.drain(client, task_key=key)
            self.assertEqual(box.status()[0]["status"], "blocked")
            self.assertEqual(box.db.execute("SELECT payload FROM tasks").fetchone()[0], self.raw)
        finally:
            box.close()

    def test_damaged_frozen_trace_is_blocked_before_any_upload(self):
        client, transport = platform([])
        box = Outbox(self.state)
        try:
            key = box.enqueue(client.base_url, "owner-1", self.metadata, self.raw, trace_raw=self.trace)
            with box.db:
                box.db.execute("UPDATE tasks SET trace_payload=NULL WHERE task_key=?", (key,))
            with self.assertRaisesRegex(BridgeError, "factory_trace_frozen_hash_mismatch"):
                box.drain(client, task_key=key)
            self.assertEqual(len(transport.requests), 1)  # mock login only
            self.assertEqual(box.status()[0]["status"], "blocked")
        finally:
            box.close()

    def test_sidecar_is_in_capacity_and_distinct_frozen_envelope(self):
        box = Outbox(self.state, max_pending_bytes=len(self.raw))
        try:
            with self.assertRaisesRegex(BridgeError, "outbox_full"):
                box.enqueue("https://platform.example", "owner-1", self.metadata, self.raw, trace_raw=self.trace)
            self.assertEqual(box.status(), [])
        finally:
            box.close()
        box = Outbox(self.state)
        try:
            a = box.enqueue("https://platform.example", "owner-1", self.metadata, self.raw, trace_raw=self.trace)
            b = box.enqueue("https://platform.example", "owner-1", self.metadata, self.raw, trace_raw=trace_bytes(self.raw, note="different frozen audit"))
            self.assertNotEqual(a, b)
        finally:
            box.close()

    def test_existing_queue_migrates_without_changing_key_or_frozen_csv(self):
        self.state.parent.mkdir(mode=0o700)
        with sqlite3.connect(self.state) as db:
            db.execute("CREATE TABLE tasks(task_key TEXT PRIMARY KEY,platform_url TEXT,owner_id TEXT,metadata TEXT,payload BLOB,bytes INTEGER,status TEXT,attempts INTEGER DEFAULT 0,last_error TEXT DEFAULT '',receipt TEXT,envelope_hash TEXT)")
            db.execute("INSERT INTO tasks(task_key,platform_url,owner_id,metadata,payload,bytes,status) VALUES(?,?,?,?,?,?,'pending')",
                       ("legacy-key", "https://platform.example", "owner-1", json.dumps(self.metadata), self.raw, len(self.raw)))
        box = Outbox(self.state)
        try:
            self.assertEqual(box.db.execute("SELECT task_key,payload,trace_payload,trace_sha256 FROM tasks").fetchone(), ("legacy-key", self.raw, None, None))
        finally:
            box.close()

    def test_invalid_shape_and_changed_csv_cells_rejected_before_queue_insert(self):
        for change in ("cell", "duplicate_record", "history", "scope", "field", "row_key", "edit"):
            value = json.loads(self.trace)
            if change == "cell":
                value["rows"][0]["verdict"] = "不通过"
            elif change == "duplicate_record":
                value["rows"].append(dict(value["rows"][0]))
            elif change == "history":
                value["edit_history"] = ["not a record"]
            elif change == "scope":
                del value["identity_scope"]
            elif change == "row_key":
                del value["rows"][0]["row_key"]
            elif change == "edit":
                value["edit_history"] = [{"sn": "wrong", "row_key": "row", "source_token": "token", "before": {}, "after": {}}]
            else:
                del value["rows"][0]["actual_result"]
            box = Outbox(self.state)
            try:
                with self.assertRaises(BridgeError):
                    box.enqueue("https://platform.example", "owner-1", self.metadata, self.raw,
                                trace_raw=json.dumps(value).encode())
                self.assertEqual(box.status(), [])
            finally:
                box.close()
        for raw in (b'{"schema":"x","schema":"y"}', b'{"value":NaN}'):
            with self.assertRaises(BridgeError):
                validate_trace(raw, self.raw)

    def test_invalid_utf8_and_non_object_manifest_rejected(self):
        for raw in (b"\xff", b"[]", b"null", b"{}"):
            with self.assertRaises(BridgeError):
                validate_trace(raw, self.raw)


def test_generated_summary_sidecar_flows_through_native_queue(tmp_path):
    from contextlib import closing

    from test_platform_native import FakePlatform

    from rp1_monitor.factory_store import FactoryStore
    from rp1_monitor.platform_workspace import NativePlatformService

    with closing(FactoryStore(tmp_path / "local")) as store:
        store.save_estop_pairing("SN-01", "fail")
        store.save_report_edits("SN-01", [], station_id="PC-01")
        path = Path(store.export_result_summary("SN-01", tmp_path / "exports"))
    raw = path.read_bytes()
    trace = read_trace(path, raw)
    assert trace and json.loads(trace)["software_version"]
    service = NativePlatformService(tmp_path / "native")
    client = FakePlatform()
    client.user = {"id": client.owner}
    calls = []

    def send_csv(metadata, data, key):
        assert metadata["trace_sha256"] == hashlib.sha256(trace).hexdigest()
        assert data == raw
        calls.append("csv")
        return archive_receipt(metadata)

    def send_trace(archive_id, data, digest):
        calls.append("trace")
        assert data == trace
        return {"id": archive_id, "trace_sha256": digest, "formal_statistics_eligible": False}

    client.upload_result, client.upload_trace = send_csv, send_trace
    service.client = client
    try:
        result = service.operation("factory_submit", {"path": str(path), "source_id": "PC-01", "robot_id": "mock-robot",
                                                      "source_kind": "simulated", "verified": True, "completed": True, "reason": "offline fixture"})
        assert calls == ["csv", "trace"]
        assert result["task"]["status"] == result["task"]["trace_status"] == "sent"
        assert service.operation("tasks")["items"][0]["trace_status"] == "sent"
    finally:
        service.close()
