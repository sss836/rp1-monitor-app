"""Offline mock HTTP contract tests; no monitor, robot, or platform is started."""

import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import BaseHandler
from urllib.response import addinfourl

from rp1_monitor.factory_bridge import (
    CSV_COLUMNS,
    BridgeError,
    JsonHttpClient,
    MonitorClient,
    Outbox,
    PlatformClient,
    RequestDeadline,
    connect_with_deadline,
    presence_payload,
    result_upload,
)


class MockHTTP(BaseHandler):
    handler_order = 100

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def http_open(self, request):
        self.requests.append(request)
        status, body, headers = self.replies.pop(0)
        message = Message()
        for key, value in headers:
            message.add_header(key, value)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status >= 400:
            raise HTTPError(request.full_url, status, "mock", message, io.BytesIO(raw))
        response = addinfourl(io.BytesIO(raw), message, request.full_url, status)
        response.msg = "mock"
        return response


def login_reply():
    return (200, {"data": {"user": {
        "id": "owner-1", "kind": "HUMAN", "must_change_password": False,
    }}}, [("Set-Cookie", "rp1_session=session-secret; Path=/; HttpOnly"),
          ("Set-Cookie", "rp1_csrf=csrf-secret; Path=/")])


def platform(replies):
    client = PlatformClient("http://127.0.0.1:19999")
    transport = MockHTTP([login_reply(), *replies])
    client.opener.add_handler(transport)
    client.login("operator", "password-secret")
    return client, transport


def archive_receipt(metadata):
    return {"id": "00000000-0000-4000-8000-000000000001", "sha256": metadata["sha256"],
            "source_id": metadata["source_id"], "robot_id": metadata["robot_id"],
            "source_kind": metadata["source_kind"], "serial_number": "SN-01",
            "size_bytes": metadata["size_bytes"], "status": "PENDING", "revision": 1,
            "formal_statistics_eligible": False}


def csv_file(directory, *, source="PC-01", serial="SN-01", model="24轴", note="人工核实"):
    import csv

    target = Path(directory) / "results.csv"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(CSV_COLUMNS)
    writer.writerow([
        "record-1", source, serial, model, "SYS", "整机工厂测试", "项目", "SYS-001",
        "", "基础自检", "2026-11-05T01:00:00+00:00", "2026-11-05T01:05:00+00:00",
        "执行员", "已核实", "人工检查", "通过", note,
    ])
    target.write_bytes(stream.getvalue().encode("utf-8-sig"))
    return target


def upload(path, **changes):
    options = dict(
        source="PC-01", robot_id="ROBOT-1", kind="real", verified=True,
        completed=True, submit=True, reason="核对SN与完整测试结果",
    )
    options.update(changes)
    return result_upload(path, **options)


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
        self.bindings = [{"robot_id": "ROBOT-1", "serial_number": "SN-01", "model": "24轴"}]
        self.snapshot = {"as_of_at": self.now.isoformat(), "items": [{
            "robot_id": "ROBOT-1", "source_kind": "ros2", "enabled": True,
            "online": True, "data_fresh": True, "received_at": self.now.isoformat(),
        }]}

    def make(self, snapshot=None, bindings=None, kind="real"):
        return presence_payload(
            "PC-01", kind, "整机工位一", self.bindings if bindings is None else bindings,
            self.snapshot if snapshot is None else snapshot, now=self.now,
        )

    def test_online_uses_explicit_binding_and_real_observation(self):
        payload, skipped = self.make()
        self.assertEqual(payload["protocol"], "reliability_v1")
        self.assertEqual(payload["source_kind"], "real")
        self.assertEqual(payload["robots"][0]["serial_number"], "SN-01")
        self.assertTrue(payload["robots"][0]["online"])
        self.assertEqual(skipped, [])

    def test_no_sn_or_model_is_never_invented(self):
        for binding in [{"robot_id": "ROBOT-1"}, {"robot_id": "ROBOT-1", "serial_number": "SN-01"}]:
            payload, skipped = self.make(bindings=[binding])
            self.assertEqual(payload["robots"], [])
            self.assertEqual(skipped, ["ROBOT-1"])

    def test_stale_snapshot_and_source_disconnect_are_offline(self):
        cases = [
            {"as_of_at": None, "items": []},
            {**self.snapshot, "as_of_at": (self.now - timedelta(seconds=60)).isoformat()},
            {**self.snapshot, "as_of_at": (self.now + timedelta(minutes=1)).isoformat()},
        ]
        for field, value in [("enabled", False), ("online", False), ("online", "true"),
                             ("received_at", None), ("source_kind", "simulated")]:
            changed = copy.deepcopy(self.snapshot)
            changed["items"][0][field] = value
            cases.append(changed)
        for snapshot in cases:
            payload, _ = self.make(snapshot=snapshot)
            self.assertFalse(payload["robots"][0]["online"])
            self.assertFalse(payload["robots"][0]["data_fresh"])

    def test_simulated_source_is_kept_separate(self):
        self.snapshot["items"][0]["source_kind"] = "simulated"
        payload, _ = self.make(kind="simulated")
        self.assertEqual(payload["source_kind"], "simulated")
        self.assertTrue(payload["robots"][0]["online"])

    def test_duplicate_monitor_identity_is_refused(self):
        self.snapshot["items"].append(self.snapshot["items"][0])
        with self.assertRaisesRegex(BridgeError, "duplicate_monitor_identity"):
            self.make()


class HttpTests(unittest.TestCase):
    def test_tls_close_race_after_deadline_is_reported_as_timeout(self):
        import threading

        for exception in [ValueError, AttributeError]:
            client = JsonHttpClient("http://127.0.0.1:19999", timeout=1)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, size):
                    client._deadline.abort()
                    raise exception("closed TLS I/O")

            class Opener:
                def open(self, request, **kwargs):
                    return Response()

            client.opener = Opener()
            with self.assertRaisesRegex(BridgeError, "request_deadline_exceeded"):
                client.request("GET", "/closed")
            self.assertFalse(any(t.name == "rp1-bridge-request-deadline" for t in threading.enumerate()))

    def test_tcp_address_fallback_shares_total_deadline_and_closes_connecting_socket(self):
        import socket
        import threading
        import time
        from unittest.mock import patch

        class ConnectingSocket:
            def __init__(self, first=False):
                self.first = first
                self.closed = threading.Event()
                self.timeout = None

            def settimeout(self, value):
                self.timeout = value

            def connect(self, address):
                self.closed.wait(0.03 if self.first else 0.4)
                raise OSError("unreachable mock address")

            def shutdown(self, how):
                self.closed.set()

            def close(self):
                self.closed.set()

        first, second = ConnectingSocket(first=True), ConnectingSocket()
        candidates = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 80))] * 3
        deadline = RequestDeadline(0.09)
        deadline.start()
        started = time.monotonic()
        try:
            with patch("socket.getaddrinfo", return_value=candidates), patch("socket.socket", side_effect=[first, second]) as create:
                with self.assertRaisesRegex(BridgeError, "request_deadline_exceeded"):
                    connect_with_deadline(("mock.invalid", 80), 1, deadline=deadline)
                self.assertEqual(create.call_count, 2)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertLess(second.timeout, first.timeout)
            self.assertTrue(first.closed.is_set())
            self.assertTrue(second.closed.is_set())
        finally:
            deadline.finish()
        self.assertFalse(any(t.name == "rp1-bridge-request-deadline" for t in threading.enumerate()))

    def slow_drip(self, *, headers):
        import socketserver
        import threading
        import time

        stop = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(1)
                try:
                    self.request.recv(8192)
                    if headers:
                        self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    else:
                        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: application/json\r\n\r\n")
                    while not stop.wait(0.02):
                        self.request.sendall(b"x")
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        try:
            server = Server(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("sandbox disallows loopback listener; run this fault test in isolated Docker")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            client = JsonHttpClient(f"http://127.0.0.1:{server.server_address[1]}", timeout=0.15)
            started = time.monotonic()
            with self.assertRaisesRegex(BridgeError, "request_deadline_exceeded"):
                client.request("GET", "/slow-drip")
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertFalse(any(t.name == "rp1-bridge-request-deadline" for t in threading.enumerate()))
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_total_deadline_interrupts_slow_drip_headers_without_worker_leak(self):
        self.slow_drip(headers=True)

    def test_total_deadline_interrupts_slow_drip_body_without_worker_leak(self):
        self.slow_drip(headers=False)

    def test_login_cookie_csrf_and_factory_only_endpoint(self):
        client, transport = platform([(200, {"data": {
            "source_id": "PC-01", "accepted": 0, "as_of_at": "2026-11-05T02:00:00Z",
            "offline_after_seconds": 45,
        }}, [])])
        client.publish_presence({"protocol": "reliability_v1", "source_id": "PC-01", "source_kind": "real", "robots": []})
        request = transport.requests[1]
        self.assertEqual(urlsplit(request.full_url).path, "/api/v1/factory/robots/presence")
        self.assertEqual(request.get_header("X-csrf-token"), "csrf-secret")
        self.assertIn("rp1_session=session-secret", request.get_header("Cookie"))
        self.assertNotIn("Authorization", request.headers)

    def test_expired_session_clears_credentials_and_does_not_relogin_automatically(self):
        client, _ = platform([(401, {"secret": "password-secret"}, [])])
        with self.assertRaisesRegex(BridgeError, "http_401"):
            client.publish_presence({})
        self.assertIsNone(client.user)
        self.assertEqual(client.csrf(), "")

    def test_redirect_is_not_followed(self):
        client = PlatformClient("http://127.0.0.1:19999")
        transport = MockHTTP([(302, b"", [("Location", "https://untrusted.example/")])])
        client.opener.add_handler(transport)
        with self.assertRaisesRegex(BridgeError, "redirect_refused"):
            client.login("operator", "password-secret")
        self.assertEqual(len(transport.requests), 1)

    def test_remote_plain_http_and_url_credentials_require_rejection(self):
        for url in ["http://remote.example", "https://user:secret@example.com", "https://example.com/?token=secret"]:
            with self.assertRaises(BridgeError):
                PlatformClient(url)

    def test_monitor_only_reads_approved_loopback_get(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "viewer"
            token.write_text("viewer-secret")
            client = MonitorClient("http://127.0.0.1:18980", token)
            transport = MockHTTP([(200, {"items": [], "as_of_at": None}, [])])
            client.opener.add_handler(transport)
            client.robots()
            self.assertEqual(transport.requests[0].get_method(), "GET")
            self.assertEqual(urlsplit(transport.requests[0].full_url).path, "/api/v1/robot-monitors")
            with self.assertRaisesRegex(BridgeError, "monitor_must_be_loopback"):
                MonitorClient("http://192.168.1.1:18980", token)

    def test_monitor_large_response_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "viewer"
            token.write_text("viewer-secret")
            client = MonitorClient("http://127.0.0.1:18980", token)
            client.opener.add_handler(MockHTTP([(200, b"x" * (2 * 1024**2 + 1), [])]))
            with self.assertRaisesRegex(BridgeError, "response_too_large"):
                client.robots()


class UploadTests(unittest.TestCase):
    def test_all_explicit_flags_are_required_before_read_or_network(self):
        for flag in ["verified", "completed", "submit"]:
            with self.assertRaisesRegex(BridgeError, "explicit_verified_completed_submit_required"):
                upload("/nonexistent.csv", **{flag: False})

    def test_original_bytes_bom_and_hash_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = csv_file(directory)
            metadata, raw = upload(path)
            self.assertEqual(raw, path.read_bytes())
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(metadata["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(metadata["verified"], "true")
            self.assertEqual(metadata["completed"], "true")

    def test_wrong_source_sn_model_and_header_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for options in [{"source": "OTHER"}, {"serial": ""}, {"model": "unknown"}]:
                with self.assertRaises(BridgeError):
                    upload(csv_file(directory, **options))
            path = csv_file(directory)
            path.write_text("a,b\n1,2\n")
            with self.assertRaisesRegex(BridgeError, "invalid_17_column_csv"):
                upload(path)

    def test_more_than_platform_1000_rows_is_refused_locally(self):
        import csv

        with tempfile.TemporaryDirectory() as directory:
            path = csv_file(directory)
            rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(rows[0])
            writer.writerows([rows[1]] * 1001)
            path.write_text(stream.getvalue(), encoding="utf-8")
            with self.assertRaisesRegex(BridgeError, "invalid_17_column_csv"):
                upload(path)

    def test_failed_upload_retains_frozen_bytes_and_same_key_then_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = csv_file(directory)
            metadata, raw = upload(path)
            receipt = archive_receipt(metadata)
            client, transport = platform([(503, {"password": "password-secret"}, []), (200, {"data": receipt}, [])])
            box = Outbox(Path(directory) / "state" / "outbox.sqlite")
            try:
                key = box.enqueue(client.base_url, "owner-1", metadata, raw)
                self.assertEqual(key, box.enqueue(client.base_url, "owner-1", metadata, raw))
                with self.assertRaisesRegex(BridgeError, "http_503"):
                    box.drain(client)
                path.write_text("modified after explicit submission")
                self.assertEqual(box.status()[0]["status"], "pending")
                box.drain(client)
                first, retry = transport.requests[1:]
                self.assertEqual(first.data, raw)
                self.assertEqual(retry.data, raw)
                self.assertEqual(first.get_header("Idempotency-key"), key)
                self.assertEqual(retry.get_header("Idempotency-key"), key)
                query = parse_qs(urlsplit(retry.full_url).query)
                self.assertEqual(query["source_id"], ["PC-01"])
                self.assertEqual(query["verified"], ["true"])
                self.assertEqual(query["completed"], ["true"])
                self.assertEqual(urlsplit(retry.full_url).path, "/api/v1/factory/robot-result-archives")
                self.assertTrue(path.exists())
                self.assertEqual(box.status()[0]["status"], "sent")
                self.assertIsNone(box.db.execute("SELECT payload FROM tasks").fetchone()[0])
            finally:
                box.close()
            stored = (Path(directory) / "state" / "outbox.sqlite").read_bytes()
            for secret in [b"password-secret", b"session-secret", b"csrf-secret", b"viewer-secret"]:
                self.assertNotIn(secret, stored)

    def test_outbox_capacity_preserves_existing_task(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            box = Outbox(Path(directory) / "state" / "outbox.sqlite", max_pending_count=1)
            try:
                key = box.enqueue("http://127.0.0.1:19999", "owner-1", metadata, raw)
                changed = {**metadata, "reason": "另一次明确核实"}
                with self.assertRaisesRegex(BridgeError, "outbox_full"):
                    box.enqueue("http://127.0.0.1:19999", "owner-1", changed, raw)
                self.assertEqual(box.status()[0]["task_key"], key)
            finally:
                box.close()

    def test_explicit_resubmission_after_sent_has_new_attempt_and_post(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            first = archive_receipt(metadata)
            second = {**first, "id": "00000000-0000-4000-8000-000000000002"}
            client, transport = platform([(200, {"data": first}, []), (200, {"data": second}, [])])
            box = Outbox(Path(directory) / "state" / "outbox.sqlite")
            try:
                first_key = box.enqueue(client.base_url, "owner-1", metadata, raw)
                box.drain(client)
                # The platform may have rejected the first archive after this ACK.
                # A fresh explicit submit must reach the server to evaluate that state.
                second_key = box.enqueue(client.base_url, "owner-1", metadata, raw)
                self.assertNotEqual(first_key, second_key)
                self.assertEqual(second_key, box.enqueue(client.base_url, "owner-1", metadata, raw))
                box.drain(client)
                posts = [request for request in transport.requests if "/robot-result-archives?" in request.full_url]
                self.assertEqual(len(posts), 2)
                self.assertNotEqual(posts[0].get_header("Idempotency-key"), posts[1].get_header("Idempotency-key"))
                self.assertEqual([row["status"] for row in box.status()], ["sent", "sent"])
            finally:
                box.close()

    def test_pending_task_cannot_be_sent_as_different_account_or_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            client, transport = platform([])
            box = Outbox(Path(directory) / "state" / "outbox.sqlite")
            try:
                box.enqueue(client.base_url, "different-owner", metadata, raw)
                with self.assertRaisesRegex(BridgeError, "outbox_account_or_destination_mismatch"):
                    box.drain(client)
                self.assertEqual(len(transport.requests), 1)
            finally:
                box.close()

    def test_extra_sensitive_metadata_is_never_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            box = Outbox(Path(directory) / "state" / "outbox.sqlite")
            try:
                with self.assertRaisesRegex(BridgeError, "invalid_submission_metadata"):
                    box.enqueue("http://127.0.0.1:19999", "owner-1", {**metadata, "password": "secret"}, raw)
                self.assertEqual(box.status(), [])
            finally:
                box.close()

    def test_unconfirmed_or_wrong_receipt_keeps_pending_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            for change in [{"sha256": "0" * 64}, {"id": "invalid"}, {"serial_number": "OTHER"},
                           {"source_id": "OTHER"}, {"robot_id": "OTHER"}, {"size_bytes": 1},
                           {"status": "DRAFT"}, {"formal_statistics_eligible": True}]:
                client, _ = platform([(200, {"data": {**archive_receipt(metadata), **change}}, [])])
                box = Outbox(Path(directory) / str(len(change) + len(str(change))) / "outbox.sqlite")
                try:
                    box.enqueue(client.base_url, "owner-1", metadata, raw)
                    with self.assertRaises(BridgeError):
                        box.drain(client)
                    self.assertEqual(box.status()[0]["status"], "pending")
                    self.assertEqual(box.db.execute("SELECT payload FROM tasks").fetchone()[0], raw)
                finally:
                    box.close()

    def test_concurrent_enqueue_cannot_exceed_shared_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        with tempfile.TemporaryDirectory() as directory:
            metadata, raw = upload(csv_file(directory))
            state = Path(directory) / "state" / "outbox.sqlite"
            Outbox(state, max_pending_count=1).close()
            barrier = Barrier(2)

            def enqueue(number):
                box = Outbox(state, max_pending_count=1)
                try:
                    barrier.wait(timeout=5)
                    return box.enqueue("http://127.0.0.1:19999", "owner-1", {**metadata, "reason": str(number)}, raw)
                except BridgeError as exc:
                    return exc.code
                finally:
                    box.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(enqueue, [1, 2]))
            self.assertEqual(sum(value.startswith("factory-result-") for value in results), 1)
            self.assertEqual(results.count("outbox_full_existing_evidence_retained"), 1)
            box = Outbox(state)
            try:
                self.assertEqual(len(box.status()), 1)
            finally:
                box.close()


if __name__ == "__main__":
    unittest.main()
