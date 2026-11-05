"""Serialized native platform operations; no Qt, robot control or source writes."""

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

from .factory_bridge import BridgeError, Outbox, private_file, result_upload, source_id, timestamp
from .platform_client import NativePlatformError, PlatformSessionClient
from .recording_export import (
    MAX_EXPORT_BYTES,
    MAX_EXPORT_DIRECTORY_BYTES,
    MAX_METADATA_BYTES,
    MIN_FREE_BYTES,
    ResearchRecordingExporter,
    cancelled,
    canonical,
)

CONTEXT_KEYS = {"campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id"}
VERIFY_KEYS = {"tester", "station_id", "verdict", "notes", "declared_started_at", "declared_ended_at", "declared_active_seconds", "declared_completed_cycles", "declared_exception_count", "discrepancy_reason"}
CHUNK_BYTES = 4 * 1024**2
MAX_PENDING_BYTES = 64 * 1024**3
READY_WAIT_SECONDS = 45


def uid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise NativePlatformError("invalid_uuid") from None


def sha256_file(path, event=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
            cancelled(event)
            digest.update(chunk)
    return digest.hexdigest()


class NativePlatformService:
    """One serialized worker owns session and operations; cancel/close are thread safe.

    Budgets describe private disk copies, not memory or measured test exposure.
    Existing source records and exports are never deleted automatically.
    """
    def __init__(self, state_dir, *, max_export_bytes=MAX_EXPORT_BYTES,
                 max_pending_bytes=MAX_PENDING_BYTES,
                 max_export_directory_bytes=MAX_EXPORT_DIRECTORY_BYTES, min_free_bytes=MIN_FREE_BYTES):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.state_dir.lstat()
        if self.state_dir.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NativePlatformError("state_directory_must_be_private")
        if type(max_pending_bytes) is not int or max_pending_bytes < 0:
            raise NativePlatformError("invalid_pending_budget")
        self.max_pending_bytes = max_pending_bytes
        self.exporter = ResearchRecordingExporter(self.state_dir / "exports", max_export_bytes=max_export_bytes,
                                                  max_directory_bytes=max_export_directory_bytes, min_free_bytes=min_free_bytes)
        self.client = None
        self._lock = threading.Lock()
        self._event = None
        self._closed = False
        self._initialize()

    @contextmanager
    def _db(self):
        path = self.state_dir / "research-tasks.sqlite"
        if path.is_symlink():
            raise NativePlatformError("invalid_state_path")
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise NativePlatformError("invalid_state_path")
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,url TEXT NOT NULL,
                domain TEXT NOT NULL CHECK(domain='RESEARCH'),source_recording_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,status TEXT NOT NULL,metadata TEXT NOT NULL,
                archive_id TEXT,last_error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)""")
        os.chmod(self.state_dir / "research-tasks.sqlite", 0o600)

    def session(self):
        return self.client.session() if self.client else {
            "authenticated": False, "user": None, "expires_at": None, "generation": 0,
            "can_submit_factory": False, "can_submit_research": False, "can_review_factory": False,
        }

    def cancel_current(self):
        event = self._event
        if event is not None:
            event.set()
        if self.client is not None:
            self.client.cancel_current_request()

    def close(self):
        self._closed = True
        self.cancel_current()
        if self._lock.acquire(blocking=False):
            try:
                if self.client:
                    self.client.clear()
            finally:
                self._lock.release()

    def _require(self, permission=None):
        session = self.session()
        if not session["authenticated"]:
            raise NativePlatformError("login_required", 401)
        if session["user"].get("must_change_password"):
            raise NativePlatformError("password_change_required", 403)
        if permission and not session.get(permission):
            raise NativePlatformError("forbidden", 403)
        cancelled(self._event)
        return str(session["user"]["id"])

    def operation(self, action, payload=None):
        """Return an action result plus session; all safe errors attach session.

        The UI freezes ``session_generation`` with non-authentication actions.
        Factory CSV submission and research export intake require separate human
        verified/completed flags; neither successful delivery nor a sealed PG
        recording asserts hardware completion, a passed result or approval.
        """
        if not self._lock.acquire(blocking=False):
            error = NativePlatformError("operation_busy")
            error.session = self.session()
            raise error
        self._event = threading.Event()
        try:
            if self._closed:
                raise NativePlatformError("service_closed")
            payload = dict(payload or {})
            if "session_generation" in payload and payload["session_generation"] != self.session()["generation"]:
                raise NativePlatformError("stale_session_operation")
            if self.client:
                self.client.cancel_event = self._event
            result = self._operation(action, payload)
            return {**result, "session": self.session()}
        except BridgeError as exc:
            error = exc if isinstance(exc, NativePlatformError) else NativePlatformError(exc.code, exc.status)
            error.session = self.session()
            raise error from None
        except Exception:
            error = NativePlatformError("local_operation_failed")
            error.session = self.session()
            raise error from None
        finally:
            if self.client:
                self.client.cancel_event = None
                if self._closed:
                    self.client.clear()
            self._event = None
            self._lock.release()

    def _operation(self, action, p):
        if action == "configure":
            if self.session()["authenticated"]:
                raise NativePlatformError("logout_before_reconfigure")
            self.client = PlatformSessionClient(p["url"], ca_file=p.get("ca_file") or None, allow_http=p.get("allow_http", False))
            self.client.cancel_event = self._event
            return {"url": self.client.base_url}
        if self.client is None:
            raise NativePlatformError("platform_not_configured")
        if action == "login":
            self.client.login(p["username"], p["password"])
            return {}
        if action == "profile":
            self.client.current_me()
            return {}
        if action == "change_password":
            self.client.change_password(p["current_password"], p["new_password"])
            return {}
        if action == "logout":
            self.client.logout()
            return {}
        self._require()
        if action == "setup":
            if self.session()["user"]["role"] == "FACTORY_REVIEWER":
                return {"catalog": [], "campaigns": [], "options": {}}
            return {name: self.client.call("GET", f"/api/v1/setup/{name}") for name in ("catalog", "campaigns", "options")}
        if action == "contexts":
            return {"items": self.client.call("GET", f"/api/v1/setup/campaigns/{uid(p['campaign_id'])}/contexts")}
        if action == "factory_results":
            return self._listing("/api/v1/factory/robot-result-archives", p, {"source_id", "robot_id", "serial_number", "status", "before", "limit"})
        if action == "research_results":
            return self._listing("/api/v1/archives", {**p, "data_domain": "RESEARCH"}, {"q", "submission_status", "before", "limit", "data_domain"})
        if action in {"factory_detail", "research_detail"}:
            prefix = "/api/v1/factory/robot-result-archives" if action == "factory_detail" else "/api/v1/archives"
            archive = self.client.call("GET", prefix + "/" + uid(p["archive_id"]))
            if action == "research_detail" and archive.get("data_domain") != "RESEARCH":
                raise NativePlatformError("archive_domain_mismatch")
            return {"archive": archive}
        if action == "factory_review":
            owner = self._require("can_review_factory")
            archive_id = uid(p["archive_id"])
            review = {key: p[key] for key in ("decision", "expected_revision", "reason")}
            fingerprint = hashlib.sha256(canonical({"owner": owner, "archive_id": archive_id, **review}).encode()).hexdigest()
            archive = self.client.call("POST", f"/api/v1/factory/robot-result-archives/{archive_id}/review",
                                       payload=review, key="native-review-" + fingerprint)
            return {"archive": archive}
        if action == "factory_submit":
            return self._factory_submit(p)
        if action == "factory_preview":
            from .factory_preview import preview_result_csv

            self._require("can_submit_factory")
            return {"preview": preview_result_csv(p["path"])}
        if action == "research_records":
            self._require("can_submit_research")
            return {"items": self.exporter.recordings(p["postgres_file"], robot_id=p.get("robot_id"), limit=p.get("limit", 200), cancel_event=self._event)}
        if action == "research_export":
            self._require("can_submit_research")
            return {"export": self.exporter.export(p["postgres_file"], p["recording_id"], robot_id=p.get("robot_id"), cancel_event=self._event)}
        if action == "research_submit":
            self._require("can_submit_research")
            task = self._enqueue_research(p)
            return self._process_research(task["task_id"])
        if action == "tasks":
            return {"items": self._tasks()}
        if action == "retry":
            return self._retry(p.get("task_id"))
        raise NativePlatformError("unsupported_platform_action")

    def _listing(self, path, p, allowed):
        query = {key: value for key, value in p.items() if key in allowed and value is not None and value != ""}
        query["limit"] = min(100, max(1, int(query.get("limit", 50))))
        return self.client.call("GET", path + "?" + urlencode(query))

    def _factory_box(self, owner, source):
        directory = self.state_dir / "factory" / uid(owner) / source_id(source)
        return Outbox(directory / "outbox.sqlite")

    def _factory_submit(self, p):
        owner = self._require("can_submit_factory")
        metadata, raw = result_upload(p["path"], source=p["source_id"], robot_id=p["robot_id"], kind=p["source_kind"],
                                      verified=p.get("verified") is True, completed=p.get("completed") is True,
                                      submit=True, reason=p["reason"])
        if p.get("expected_sha256") is not None and p["expected_sha256"] != metadata["sha256"]:
            raise NativePlatformError("factory_csv_changed_since_preview")
        cancelled(self._event)
        box = self._factory_box(owner, metadata["source_id"])
        try:
            key = box.enqueue(self.client.base_url, owner, metadata, raw)
            receipts = box.drain(self.client, source=metadata["source_id"])
            receipt = next((item["receipt"] for item in receipts if item["task_key"] == key), None)
            return {"task": {"task_id": key, "domain": "FACTORY", "status": "sent" if receipt else "pending", "owner_id": owner,
                             "filename": metadata["filename"], "sha256": metadata["sha256"], "archive_id": receipt.get("id") if receipt else None}, "receipt": receipt}
        finally:
            box.close()

    def _export(self, export_id):
        folder = self.state_dir / "exports" / uid(export_id)
        if folder.is_symlink() or (folder / "raw.csv").is_symlink() or (folder / "manifest.json").is_symlink():
            raise NativePlatformError("invalid_export_path")
        meta = json.loads(private_file(folder / "manifest.json", MAX_METADATA_BYTES + 8192))
        path = folder / "raw.csv"
        if meta.get("export_id") != str(export_id) or path.stat().st_size != meta["size_bytes"] or sha256_file(path, self._event) != meta["sha256"]:
            raise NativePlatformError("export_evidence_changed")
        return {**meta, "path": str(path)}

    def _verification(self, p, context):
        value = p.get("verification")
        required = VERIFY_KEYS - {"discrepancy_reason"}
        if not isinstance(value, dict) or set(value) - VERIFY_KEYS or not required <= value.keys():
            raise NativePlatformError("explicit_research_verification_required")
        value = {key: item.strip() if isinstance(item, str) else item for key, item in value.items()}
        if any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("tester", "notes")) or value.get("verdict") not in {"passed", "failed", "blocked"}:
            raise NativePlatformError("explicit_research_verification_required")
        if len(value["tester"]) > 100 or len(value["notes"]) > 1000 or not isinstance(value.get("discrepancy_reason", ""), str) or len(value.get("discrepancy_reason", "")) > 1000:
            raise NativePlatformError("research_verification_too_long")
        value["station_id"] = uid(value["station_id"])
        if value["station_id"] != context["station_id"]:
            raise NativePlatformError("verification_station_context_mismatch")
        start, end = timestamp(value["declared_started_at"]), timestamp(value["declared_ended_at"])
        if start is None or end is None or end < start:
            raise NativePlatformError("invalid_declared_time")
        for name in ("declared_active_seconds", "declared_completed_cycles", "declared_exception_count"):
            number = value[name]
            maximum = 315360000 if name == "declared_active_seconds" else 2147483647
            if isinstance(number, bool) or not isinstance(number, (float, int)) or not 0 <= number <= maximum:
                raise NativePlatformError("explicit_research_numbers_required")
        if value["declared_active_seconds"] > (end - start).total_seconds() + .001:
            raise NativePlatformError("invalid_declared_time")
        if any(type(value[k]) is not int for k in ("declared_completed_cycles", "declared_exception_count")):
            raise NativePlatformError("explicit_research_numbers_required")
        if not p.get("reason") or not isinstance(p["reason"], str) or len(p["reason"]) > 300:
            raise NativePlatformError("submission_reason_required")
        value["reason"] = p["reason"].strip()
        return value

    def _enqueue_research(self, p):
        owner = self._require("can_submit_research")
        if p.get("verified") is not True or p.get("completed") is not True:
            raise NativePlatformError("explicit_verified_completed_submit_required")
        context = p.get("context")
        if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
            raise NativePlatformError("complete_research_context_required")
        context = {key: uid(value) for key, value in context.items()}
        verification = self._verification(p, context)
        export = self._export(p["export_id"])
        if export.get("source_kind") != "ros2":
            raise NativePlatformError("research_requires_real_source")
        for key in ("test_name", "actual_object"):
            if not isinstance(p.get(key), str) or not p[key].strip() or len(p[key]) > 200:
                raise NativePlatformError("research_identity_required")
        metadata = {"export_id": export["export_id"], "path": export["path"], "filename": export["filename"], "sha256": export["sha256"],
                    "size_bytes": export["size_bytes"], "context": context, "verification": verification,
                    "test_name": p["test_name"].strip(), "actual_object": p["actual_object"].strip(), "reason": p["reason"].strip()}
        fingerprint = hashlib.sha256(canonical({"owner": owner, "url": self.client.base_url, "domain": "RESEARCH", **metadata}).encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT task_id FROM tasks WHERE owner_id=? AND url=? AND fingerprint=? AND status!='sent'", (owner, self.client.base_url, fingerprint)).fetchone()
            if old:
                return {"task_id": old["task_id"]}
            rows = db.execute("SELECT metadata FROM tasks WHERE status!='sent'").fetchall()
            if len(rows) >= 128 or sum(json.loads(row["metadata"])["size_bytes"] for row in rows) + export["size_bytes"] > self.max_pending_bytes:
                raise NativePlatformError("research_outbox_full")
            task_id = str(uuid4())
            db.execute("INSERT INTO tasks(task_id,owner_id,url,domain,source_recording_id,fingerprint,status,metadata,created_at) VALUES(?,?,?,'RESEARCH',?,?,'pending',?,?)",
                       (task_id, owner, self.client.base_url, export["recording_id"], fingerprint, canonical(metadata), datetime.now(timezone.utc).isoformat()))
        return {"task_id": task_id}

    def _update(self, task_id, **values):
        allowed = {"status", "metadata", "archive_id", "last_error"}
        if not values or set(values) - allowed:
            raise NativePlatformError("invalid_task_update")
        with self._db() as db:
            db.execute("UPDATE tasks SET " + ",".join(key + "=?" for key in values) + " WHERE task_id=?", [*values.values(), task_id])

    def _task(self, row):
        meta = json.loads(row["metadata"])
        return {"task_id": row["task_id"], "domain": "RESEARCH", "status": row["status"], "owner_id": row["owner_id"],
                "source_recording_id": row["source_recording_id"], "filename": meta["filename"], "sha256": meta["sha256"],
                "archive_id": row["archive_id"], "last_error": row["last_error"]}

    def _tasks(self):
        owner = self._require()
        with self._db() as db:
            result = [self._task(row) for row in db.execute("SELECT * FROM tasks WHERE owner_id=? AND url=? ORDER BY created_at DESC LIMIT 512", (owner, self.client.base_url))]
        directory = self.state_dir / "factory" / uid(owner)
        if directory.exists():
            for path in sorted(directory.glob("*/outbox.sqlite"))[:100]:
                box = Outbox(path)
                try:
                    for row in box.db.execute("SELECT * FROM tasks WHERE owner_id=? AND platform_url=?", (owner, self.client.base_url)):
                        meta = json.loads(row[3])
                        receipt = json.loads(row[9]) if row[9] else {}
                        result.append({"task_id": row[0], "domain": "FACTORY", "status": row[6], "owner_id": owner, "source_id": meta["source_id"],
                                       "filename": meta["filename"], "sha256": meta["sha256"], "archive_id": receipt.get("id"), "last_error": row[8]})
                finally:
                    box.close()
        return result

    def _check_archive(self, detail, meta, owner):
        if (detail.get("sha256") != meta["sha256"] or detail.get("size_bytes") != meta["size_bytes"]
                or detail.get("data_domain") != "RESEARCH" or detail.get("origin") != "HISTORY"
                or detail.get("is_owner") is not True or str(detail.get("owner_public_id")) != owner):
            raise NativePlatformError("archive_identity_or_owner_mismatch")
        archive_id = uid(detail.get("id"))
        execution_uuid = uid(detail.get("execution_uuid"))
        identity = meta.get("archive_identity")
        if identity and (archive_id != identity["id"] or execution_uuid != identity["execution_uuid"]):
            raise NativePlatformError("archive_identity_or_owner_mismatch")

    def _check_submission_evidence(self, detail, meta):
        classification = detail.get("classification")
        actual = classification if isinstance(classification, dict) else None
        if (not isinstance(actual, dict) or any(str(actual.get(key)) != value for key, value in meta["context"].items())
                or detail.get("test_name") != meta["test_name"] or detail.get("actual_object") != meta["actual_object"]):
            raise NativePlatformError("archive_context_conflict", 409)
        verification = detail.get("current_verification")
        if not isinstance(verification, dict):
            raise NativePlatformError("archive_verification_conflict", 409)
        for key, expected in meta["verification"].items():
            # reason is an audit column; discrepancy_reason is server-derived.
            if key in {"reason", "discrepancy_reason"}:
                continue
            actual_value = verification.get(key, "" if key == "discrepancy_reason" else None)
            if key in {"declared_started_at", "declared_ended_at"}:
                equal = timestamp(actual_value) == timestamp(expected)
            else:
                equal = actual_value == expected
            if not equal:
                raise NativePlatformError("archive_verification_conflict", 409)

    def _process_research(self, task_id):
        owner = self._require("can_submit_research")
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (uid(task_id),)).fetchone()
        if row is None or row["owner_id"] != owner or row["url"] != self.client.base_url:
            raise NativePlatformError("task_owner_or_destination_mismatch")
        meta = json.loads(row["metadata"])
        archive_id = row["archive_id"]
        try:
            path = Path(meta["path"])
            if path.is_symlink() or path.stat().st_size != meta["size_bytes"] or sha256_file(path, self._event) != meta["sha256"]:
                raise NativePlatformError("export_evidence_changed")
            self._require("can_submit_research")
            if archive_id:
                detail = self.client.call("GET", f"/api/v1/archives/{uid(archive_id)}")
                if uid(detail.get("id")) != archive_id:
                    raise NativePlatformError("archive_identity_or_owner_mismatch")
            else:
                created = self.client.call("POST", "/api/v1/archive-imports", payload={
                    "data_domain": "RESEARCH", "filename": meta["filename"], "size_bytes": meta["size_bytes"], "sha256": meta["sha256"],
                    "test_name": meta["test_name"], "actual_object": meta["actual_object"], "station_id": meta["verification"]["station_id"], "classification": None,
                }, key=task_id + ":create")
                detail = created["archive"]
                self._check_archive(detail, meta, owner)
                archive_id = uid(detail["id"])
                meta["archive_identity"] = {"id": archive_id, "execution_uuid": uid(detail["execution_uuid"])}
                self._update(task_id, archive_id=archive_id, metadata=canonical(meta))
            self._check_archive(detail, meta, owner)
            if detail.get("submission_status") not in {"PENDING", "APPROVED"}:
                with path.open("rb") as file:
                    offset = int(detail["received_bytes"])
                    if not 0 <= offset <= meta["size_bytes"]:
                        raise NativePlatformError("invalid_archive_offset")
                    file.seek(offset)
                    while offset < meta["size_bytes"]:
                        self._require("can_submit_research")
                        chunk = file.read(min(CHUNK_BYTES, meta["size_bytes"] - offset))
                        if not chunk:
                            raise NativePlatformError("export_evidence_changed")
                        detail = self.client.call("PUT", f"/api/v1/archives/{archive_id}/content?offset={offset}", body=chunk, key=f"{task_id}:chunk:{offset}")
                        self._check_archive(detail, meta, owner)
                        expected = offset + len(chunk)
                        if detail.get("received_bytes") != expected:
                            raise NativePlatformError("invalid_archive_offset")
                        offset = expected
                        self._update(task_id, status="uploading", last_error="")
                if detail.get("upload_state") != "COMPLETE":
                    detail = self.client.call("POST", f"/api/v1/archives/{archive_id}/complete", payload={}, key=task_id + ":complete")
                    self._check_archive(detail, meta, owner)
                self._update(task_id, status="validating", last_error="")
                until = time.monotonic() + READY_WAIT_SECONDS
                while detail.get("processing_state") != "READY":
                    self._require("can_submit_research")
                    if detail.get("processing_state") == "FAILED":
                        raise NativePlatformError("archive_processing_failed")
                    if time.monotonic() >= until:
                        return {"task": {**self._task(row), "archive_id": archive_id, "status": "validating"}, "archive": detail}
                    if self._event.wait(0.5):
                        raise NativePlatformError("cancelled")
                    detail = self.client.call("GET", f"/api/v1/archives/{archive_id}")
                    self._check_archive(detail, meta, owner)
                frozen = meta.get("intake")
                if not (frozen and int(detail.get("submission_revision") or 0) > frozen["expected_submission_revision"]):
                    if frozen is None:
                        frozen = {"verification": {**meta["verification"], "expected_revision": int(detail["revision"])}, "context": meta["context"],
                                  "expected_submission_revision": int(detail.get("submission_revision") or 0)}
                        meta["intake"] = frozen
                        self._update(task_id, metadata=canonical(meta))
                    detail = self.client.call("POST", f"/api/v1/archives/{archive_id}/verify-and-submit", payload=frozen, key=task_id + ":intake")
                    self._check_archive(detail, meta, owner)
            if not detail.get("submission_id") or detail.get("submission_status") not in {"PENDING", "APPROVED", "REJECTED"}:
                raise NativePlatformError("archive_submission_unconfirmed")
            uid(detail["submission_id"])
            revision = detail.get("submission_revision")
            if type(revision) is not int or revision < 1 or (meta.get("intake") and revision <= meta["intake"]["expected_submission_revision"]):
                raise NativePlatformError("archive_submission_unconfirmed")
            self._check_submission_evidence(detail, meta)
            self._update(task_id, status="sent", last_error="")
            return {"task": {**self._task(row), "archive_id": archive_id, "status": "sent"}, "archive": detail}
        except BridgeError as exc:
            self._update(task_id, last_error=exc.code)
            raise

    def _retry(self, task_id=None):
        self._require()
        items = [item for item in self._tasks() if item["status"] != "sent" and (task_id is None or item["task_id"] == task_id)]
        results = []
        for item in items[:10]:
            cancelled(self._event)
            if item["domain"] == "RESEARCH":
                results.append(self._process_research(item["task_id"]))
            else:
                self._require("can_submit_factory")
                box = self._factory_box(item["owner_id"], item["source_id"])
                try:
                    results.extend(box.drain(self.client, source=item["source_id"]))
                finally:
                    box.close()
        return {"items": self._tasks(), "results": results}
