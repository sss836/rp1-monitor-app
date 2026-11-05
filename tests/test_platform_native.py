"""Pure offline native engine contracts; never connect to devices or PostgreSQL."""

import copy
import csv
import hashlib
import json
import tempfile
import threading
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_factory_bridge import MockHTTP, csv_file

from rp1_monitor.platform_client import NativePlatformError, PlatformSessionClient
from rp1_monitor.platform_workspace import CONTEXT_KEYS, NativePlatformService
from rp1_monitor.recording_export import PREFIX, ResearchRecordingExporter, canonical

OWNER = "00000000-0000-4000-8000-000000000001"
ARCHIVE = "00000000-0000-4000-8000-000000000002"


def user(must_change=False, role="TEST_EXECUTOR"):
    return {"id": OWNER, "kind": "HUMAN", "role": role, "username": "operator",
            "display_name": "执行员", "must_change_password": must_change}


def login_reply(must_change=False, role="TEST_EXECUTOR"):
    return (200, {"data": {"user": user(must_change, role),
                          "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}},
            [("Set-Cookie", "rp1_session=session-secret; Path=/; HttpOnly"),
             ("Set-Cookie", "rp1_csrf=csrf-secret; Path=/")])


def source_fixture():
    start = datetime(2026, 11, 5, 0, tzinfo=timezone.utc)
    record = {"id": str(uuid4()), "robot_id": "robot-1", "source_kind": "ros2", "started_at": start,
              "ended_at": start + timedelta(seconds=5), "metrics": ["电流", "temperature"],
              "source_context": {"catalog": [{"key": "电流", "unit": "A", "aggregation": "mean"}]}}
    rows = []
    for sequence, seconds, kind, connected, values in [(1, 1, "ros2", True, {"电流": 1.25}),
                                                       (3, 4, None, False, {"电流": None, "temperature": None})]:
        observed = (start + timedelta(seconds=seconds)).isoformat()
        payload = {"observed_at": observed, "source_kind": kind, "connected": connected, "values": values,
                   "time_basis": "pc_sampled", "source_at": None, "clock_quality": "unknown",
                   "ages_ms": {}, "missing_reasons": {"temperature": "missing"}}
        rows.append({"sequence": sequence, "observed_at": observed, "payload": payload,
                     "payload_hash": hashlib.sha256(canonical(payload).encode()).hexdigest()})
    return record, rows


class SessionTests(unittest.TestCase):
    def client(self, replies):
        client = PlatformSessionClient("http://127.0.0.1:19999")
        transport = MockHTTP(replies)
        client.opener.add_handler(transport)
        return client, transport

    def test_initial_change_password_then_profile_and_logout(self):
        normal = login_reply()[1]["data"]
        client, transport = self.client([login_reply(True), (200, {"data": user()}, []),
                                         (200, {"data": normal}, []), (204, b"", [])])
        session = client.login("operator", "login-secret")
        self.assertTrue(session["authenticated"])
        self.assertFalse(session["can_submit_research"])
        client.change_password("login-secret", "changed-secret")
        self.assertTrue(client.session()["can_submit_research"])
        self.assertEqual(transport.requests[1].get_header("X-csrf-token"), "csrf-secret")
        client.logout()
        self.assertFalse(client.session()["authenticated"])
        self.assertEqual(list(client.cookies), [])

    def test_403_preserves_session_401_expires_and_codes_are_real(self):
        client, _ = self.client([login_reply(), (403, {"error": {"code": "factory_scope_required"}}, []),
                                 (401, {"error": {"code": "session_expired"}}, [])])
        client.login("operator", "secret")
        with self.assertRaisesRegex(NativePlatformError, "factory_scope_required"):
            client.call("GET", "/api/v1/archives")
        self.assertTrue(client.session()["authenticated"])
        with self.assertRaisesRegex(NativePlatformError, "session_expired"):
            client.call("GET", "/api/v1/archives")
        self.assertFalse(client.session()["authenticated"])

    def test_expiry_and_cancellation_prevent_network(self):
        client, transport = self.client([login_reply()])
        client.login("operator", "secret")
        event = threading.Event()
        event.set()
        client.cancel_event = event
        with self.assertRaisesRegex(NativePlatformError, "cancelled"):
            client.call("GET", "/api/v1/auth/me")
        self.assertEqual(len(transport.requests), 1)
        client.cancel_event = None
        client.expires_at = datetime.now(timezone.utc).isoformat()
        self.assertFalse(client.session()["authenticated"])

    def test_machine_principal_or_missing_expiry_cannot_become_human_session(self):
        for field, value in [("kind", "MCP"), ("id", "not-a-uuid")]:
            reply = login_reply()
            reply[1]["data"]["user"][field] = value
            client, _ = self.client([reply])
            with self.assertRaisesRegex(NativePlatformError, "invalid_login_response"):
                client.login("account", "secret")
            self.assertFalse(client.session()["authenticated"])
        reply = login_reply()
        reply[1]["data"].pop("expires_at")
        client, _ = self.client([reply])
        with self.assertRaisesRegex(NativePlatformError, "invalid_login_response"):
            client.login("account", "secret")

    def test_native_reads_legal_detail_larger_than_default_bridge_budget(self):
        detail = {"rows": [{"values": ["x" * (3 * 1024**2)]}]}
        client, _ = self.client([login_reply(), (200, {"data": detail}, [])])
        client.login("operator", "secret")
        self.assertEqual(client.response_bytes, 64 * 1024**2)
        self.assertEqual(client.call("GET", "/api/v1/factory/robot-result-archives/" + ARCHIVE), detail)

    def test_response_32_and_64_mib_budgets_stop_read_at_limit_plus_one(self):
        from rp1_monitor.factory_bridge import BridgeError, JsonHttpClient

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, count):
                self.read_size = count
                return b"x" * count

        for budget in (32 * 1024**2, 64 * 1024**2):
            client = JsonHttpClient("http://127.0.0.1:19999", response_bytes=budget)
            response = Response()
            with patch.object(client.opener, "open", return_value=response):
                with self.assertRaisesRegex(BridgeError, "response_too_large"):
                    client.request("GET", "/too-large")
            self.assertEqual(response.read_size, budget + 1)


class ExportTests(unittest.TestCase):
    def test_disk_file_and_total_budgets_fail_without_partial_export(self):
        for options, code in [({"max_export_bytes": 1}, "recording_export_size_limit"),
                              ({"max_directory_bytes": 1}, "recording_export_directory_full"),
                              ({"min_free_bytes": 1000}, "recording_export_disk_reserve")]:
            with tempfile.TemporaryDirectory() as directory:
                record, rows = source_fixture()
                usage = namedtuple("usage", "total used free")(1000000, 999999, 1)
                with patch("rp1_monitor.recording_export.shutil.disk_usage", return_value=usage):
                    with self.assertRaisesRegex(NativePlatformError, code):
                        ResearchRecordingExporter(directory, **options).export_rows(record, rows, 2, 3)
                self.assertFalse(list(Path(directory).glob(".export-*")))

    def test_cancellation_preserves_source_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            record, rows = source_fixture()
            event = threading.Event()
            event.set()
            original = copy.deepcopy(rows)
            with self.assertRaisesRegex(NativePlatformError, "cancelled"):
                ResearchRecordingExporter(directory).export_rows(record, rows, 2, 3, cancel_event=event)
            self.assertEqual(rows, original)
            self.assertFalse(list(Path(directory).glob(".export-*")))

    def test_exact_payload_hash_time_gap_null_and_units_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            record, rows = source_fixture()
            result = ResearchRecordingExporter(directory).export_rows(record, iter(rows), 2, 3)
            with Path(result["path"]).open(newline="", encoding="utf-8") as file:
                data = list(csv.reader(file))
            self.assertEqual(data[0], PREFIX + record["metrics"])
            self.assertEqual(data[1][0:2], ["1.0", "1"])
            self.assertEqual(data[2][0:2], ["4.0", "3"])
            self.assertEqual(data[2][2], "")
            self.assertEqual(data[2][-2:], ["", ""])
            self.assertEqual(json.loads(data[1][3])["payload"], rows[0]["payload"])
            self.assertEqual(json.loads(data[1][2])["source_context"], record["source_context"])
            self.assertEqual(result["sequence_gap_count"], 1)
            self.assertEqual(result["sha256"], hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest())
            self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)

    def test_invalid_source_is_not_repaired_and_staging_is_removed(self):
        for mutation, code in [(lambda r, s: r.update(ended_at=None), "recording_not_ended"),
                                 (lambda r, s: r.update(source_kind="simulated"), "research_requires_real_source"),
                                 (lambda r, s: s[0].update(payload_hash="0" * 64), "source_payload_hash_mismatch"),
                                 (lambda r, s: s[1].update(sequence=1), "invalid_source_sequence")]:
            with tempfile.TemporaryDirectory() as directory:
                record, rows = source_fixture()
                mutation(record, rows)
                with self.assertRaisesRegex(NativePlatformError, code):
                    ResearchRecordingExporter(directory).export_rows(record, rows, 2, 3)
                self.assertFalse(list(Path(directory).glob(".export-*")))

    def test_value_bool_nonfinite_wrong_clock_and_sample_budget_fail_closed(self):
        for change in [{"values": {"电流": True}}, {"values": {"电流": 1e101}},
                      {"connected": 1}, {"time_basis": "device"}, {"extra": "x" * (256 * 1024)}]:
            with tempfile.TemporaryDirectory() as directory:
                record, rows = source_fixture()
                rows[0]["payload"].update(change)
                rows[0]["payload_hash"] = hashlib.sha256(canonical(rows[0]["payload"]).encode()).hexdigest()
                with self.assertRaises(NativePlatformError):
                    ResearchRecordingExporter(directory).export_rows(record, rows, 2, 3)


class FakePlatform:
    def __init__(self):
        self.base_url = "https://platform.example"
        self.cancel_event = None
        self.calls = []
        self.owner = OWNER
        self.detail = None
        self.fail_content_once = False
        self.lose_content_ack_once = False
        self.fail_intake_once = False
        self.new_import = False

    def session(self):
        return {"authenticated": True, "user": {**user(), "id": self.owner}, "expires_at": None, "generation": 2,
                "can_submit_factory": True, "can_submit_research": True, "can_review_factory": True}

    def cancel_current_request(self):
        pass

    def clear(self):
        pass

    def call(self, method, path, *, payload=None, body=None, key=None):
        self.calls.append((method, path, copy.deepcopy(payload), bytes(body or b""), key))
        if path == "/api/v1/archive-imports":
            if self.detail is None or self.new_import:
                self.detail = {"id": str(uuid4()), "execution_uuid": str(uuid4()), "owner_public_id": self.owner, "is_owner": True,
                               "data_domain": "RESEARCH", "origin": "HISTORY", "sha256": payload["sha256"],
                               "test_name": payload["test_name"], "actual_object": payload["actual_object"],
                               "size_bytes": payload["size_bytes"], "received_bytes": 0, "upload_state": "UPLOADING",
                               "processing_state": "UPLOADING", "revision": 1, "submission_revision": 0,
                               "submission_id": None, "submission_status": None}
            return {"archive": dict(self.detail), "reused": False}
        if method == "GET":
            return dict(self.detail)
        if "/content?offset=" in path:
            if self.fail_content_once:
                self.fail_content_once = False
                raise NativePlatformError("network_error")
            self.detail["received_bytes"] += len(body)
            if self.lose_content_ack_once:
                self.lose_content_ack_once = False
                raise NativePlatformError("network_error")
        elif path.endswith("/complete"):
            self.detail.update(upload_state="COMPLETE", processing_state="READY", revision=2)
        elif path.endswith("/verify-and-submit"):
            verification = {key: value for key, value in payload["verification"].items() if key not in {"expected_revision", "reason"}}
            verification["discrepancy_reason"] = "服务端按差异生成"
            self.detail.update(submission_id=str(uuid4()), submission_status="PENDING",
                               classification={**payload["context"], "revision": 1}, current_verification=verification,
                               submission_revision=self.detail["submission_revision"] + 1)
            if self.fail_intake_once:
                self.fail_intake_once = False
                raise NativePlatformError("network_error")
        return dict(self.detail)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativePlatformService(self.temp.name)
        self.service.client = FakePlatform()
        record, rows = source_fixture()
        self.export = self.service.exporter.export_rows(record, rows, 2, 3)
        self.context = {key: str(uuid4()) for key in CONTEXT_KEYS}
        self.payload = {"export_id": self.export["export_id"], "context": self.context,
                        "verification": {"tester": "人工核实人", "station_id": self.context["station_id"],
                                         "verdict": "passed", "notes": "已逐项核实，不由曲线时长推断",
                                         "declared_started_at": "2026-11-05T00:00:00+00:00",
                                         "declared_ended_at": "2026-11-05T00:00:05+00:00",
                                         "declared_active_seconds": 4.0, "declared_completed_cycles": 1,
                                         "declared_exception_count": 0}, "test_name": "测试", "actual_object": "robot-1",
                        "reason": "人工核实完整原始记录", "verified": True, "completed": True}

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_frozen_outbox_resume_offset_and_intake(self):
        self.service.client.fail_content_once = True
        with self.assertRaisesRegex(NativePlatformError, "network_error") as error:
            self.service.operation("research_submit", self.payload)
        self.assertTrue(error.exception.session["authenticated"])
        tasks = self.service.operation("tasks")["items"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "pending")
        retried = self.service.operation("retry", {"task_id": tasks[0]["task_id"]})
        result = retried["results"][0]
        self.assertEqual(result["task"]["status"], "sent")
        self.assertEqual(retried["items"][0]["task_id"], tasks[0]["task_id"])
        self.assertEqual(retried["items"][0]["domain"], "RESEARCH")
        self.assertEqual(retried["items"][0]["status"], "sent")
        self.assertNotIn("task", retried["items"][0])
        calls = self.service.client.calls
        intake = next(c[2] for c in calls if c[1].endswith("verify-and-submit"))
        self.assertEqual(intake["context"], self.context)
        self.assertEqual(intake["verification"]["declared_active_seconds"], 4.0)
        self.assertEqual(intake["verification"]["expected_revision"], 2)
        self.assertEqual(len([c for c in calls if c[1] == "/api/v1/archive-imports"]), 1)
        self.assertFalse(any("password" in str(c) for c in self.service.operation("tasks")["items"]))

    def test_lost_intake_receipt_never_sends_second_version(self):
        self.service.client.fail_intake_once = True
        with self.assertRaises(NativePlatformError):
            self.service.operation("research_submit", self.payload)
        task = self.service.operation("tasks")["items"][0]
        self.service.operation("retry", {"task_id": task["task_id"]})
        self.assertEqual(len([c for c in self.service.client.calls if c[1].endswith("verify-and-submit")]), 1)

    def test_lost_chunk_receipt_resumes_from_server_offset(self):
        self.service.client.lose_content_ack_once = True
        with self.assertRaises(NativePlatformError):
            self.service.operation("research_submit", self.payload)
        task = self.service.operation("tasks")["items"][0]
        self.assertEqual(self.service.client.detail["received_bytes"], self.export["size_bytes"])
        result = self.service.operation("retry", {"task_id": task["task_id"]})["results"][0]
        self.assertEqual(result["task"]["status"], "sent")
        self.assertEqual(len([c for c in self.service.client.calls if "/content?offset=" in c[1]]), 1)

    def test_pending_retry_reuses_key_rejected_explicit_attempt_is_new(self):
        self.service.client.fail_content_once = True
        with self.assertRaises(NativePlatformError):
            self.service.operation("research_submit", self.payload)
        original = self.service.operation("tasks")["items"][0]["task_id"]
        result = self.service.operation("research_submit", self.payload)
        self.assertEqual(result["task"]["task_id"], original)
        self.service.client.detail.update(submission_status="REJECTED")
        result = self.service.operation("research_submit", self.payload)
        self.assertNotEqual(result["task"]["task_id"], original)
        self.assertEqual(result["archive"]["submission_revision"], 2)

    def test_owner_generation_evidence_and_declarations_are_guarded(self):
        with self.assertRaisesRegex(NativePlatformError, "stale_session_operation"):
            self.service.operation("tasks", {"session_generation": 1})
        incomplete = copy.deepcopy(self.payload)
        del incomplete["verification"]["declared_active_seconds"]
        with self.assertRaisesRegex(NativePlatformError, "explicit_research_verification_required"):
            self.service.operation("research_submit", incomplete)
        self.service.client.fail_content_once = True
        with self.assertRaises(NativePlatformError):
            self.service.operation("research_submit", self.payload)
        task_id = self.service.operation("tasks")["items"][0]["task_id"]
        self.service.client.owner = str(uuid4())
        self.assertEqual(self.service.operation("tasks")["items"], [])
        with self.assertRaisesRegex(NativePlatformError, "task_owner_or_destination_mismatch"):
            self.service._process_research(task_id)
        self.service.client.owner = OWNER
        Path(self.export["path"]).write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(NativePlatformError, "export_evidence_changed"):
            self.service.operation("retry", {"task_id": task_id})

    def test_factory_task_receipt_dto_and_session_never_persist_secrets(self):
        from rp1_monitor.factory_bridge import result_upload
        path = csv_file(self.temp.name)
        metadata, raw = result_upload(path, source="PC-01", robot_id="robot-1", kind="real",
                                      verified=True, completed=True, submit=True, reason="核实")
        box = self.service._factory_box(OWNER, "PC-01")
        key = box.enqueue(self.service.client.base_url, OWNER, metadata, raw)
        box.close()
        self.assertEqual(self.service.operation("tasks")["items"][0]["task_id"], key)
        raw_state = (Path(self.temp.name) / "research-tasks.sqlite").read_bytes()
        self.assertNotIn(b"session-secret", raw_state)

    def test_pending_budget_and_protocol_identity_are_fail_closed(self):
        self.service.max_pending_bytes = 1
        with self.assertRaisesRegex(NativePlatformError, "research_outbox_full"):
            self.service.operation("research_submit", self.payload)
        self.assertEqual(self.service.operation("tasks")["items"], [])
        self.service.max_pending_bytes = 64 * 1024**3
        self.service.client.fail_content_once = True
        with self.assertRaises(NativePlatformError):
            self.service.operation("research_submit", self.payload)
        task = self.service.operation("tasks")["items"][0]
        self.service.client.detail["sha256"] = "0" * 64
        with self.assertRaisesRegex(NativePlatformError, "archive_identity_or_owner_mismatch"):
            self.service.operation("retry", {"task_id": task["task_id"]})
        self.assertNotEqual(self.service.operation("tasks")["items"][0]["status"], "sent")

    def test_archive_id_and_execution_uuid_are_frozen_across_retry(self):
        for field in ("id", "execution_uuid"):
            self.service.client.detail = None
            self.service.client.fail_content_once = True
            with self.assertRaises(NativePlatformError):
                self.service.operation("research_submit", self.payload)
            task = next(item for item in self.service.operation("tasks")["items"] if item["last_error"] == "network_error")
            self.service.client.detail[field] = str(uuid4())
            with self.assertRaisesRegex(NativePlatformError, "archive_identity_or_owner_mismatch"):
                self.service.operation("retry", {"task_id": task["task_id"]})
            # Use a fresh export/task envelope for the next independent case.
            record, rows = source_fixture()
            self.payload["export_id"] = self.service.exporter.export_rows(record, rows, 2, 3)["export_id"]

    def test_existing_pending_context_or_verification_never_silently_changes(self):
        self.service.operation("research_submit", self.payload)
        changed = copy.deepcopy(self.payload)
        changed["context"]["asset_id"] = str(uuid4())
        with self.assertRaisesRegex(NativePlatformError, "archive_context_conflict"):
            self.service.operation("research_submit", changed)
        changed = copy.deepcopy(self.payload)
        changed["verification"]["declared_active_seconds"] = 3.0
        with self.assertRaisesRegex(NativePlatformError, "archive_verification_conflict"):
            self.service.operation("research_submit", changed)
        self.assertEqual(len([c for c in self.service.client.calls if c[1].endswith("verify-and-submit")]), 1)
        self.assertEqual(len([task for task in self.service.operation("tasks")["items"] if task["status"] != "sent"]), 2)

    def test_factory_review_retries_same_frozen_request_with_same_key(self):
        payload = {"archive_id": ARCHIVE, "decision": "APPROVE", "expected_revision": 1, "reason": "人工复核"}
        with patch.object(self.service.client, "call", return_value={"id": ARCHIVE}) as call:
            self.service.operation("factory_review", payload)
            self.service.operation("factory_review", payload)
            self.assertEqual(call.call_args_list[0].kwargs["key"], call.call_args_list[1].kwargs["key"])
            self.service.operation("factory_review", {**payload, "reason": "更正说明"})
            self.assertNotEqual(call.call_args_list[0].kwargs["key"], call.call_args_list[2].kwargs["key"])

    def test_factory_reviewer_or_viewer_cannot_read_research_source_pg(self):
        original_session = self.service.client.session()
        restricted = {**original_session, "can_submit_research": False}
        with patch.object(self.service.client, "session", return_value=restricted):
            with patch.object(self.service.exporter, "recordings") as recordings:
                for action in ("research_records", "research_export"):
                    with self.assertRaisesRegex(NativePlatformError, "forbidden"):
                        self.service.operation(action, {"postgres_file": "/not/read"})
                recordings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
