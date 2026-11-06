"""Native UI tests use a mock platform and temporary local store, never devices."""

import csv
import io
import os
import shutil
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QCoreApplication, QDateTime, QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication

from rp1_monitor.desktop_platform import (
    _WORKERS,
    LazyPlatformService,
    PlatformMonitorWindow,
    platform_link,
    platform_state_path,
)


def session(*, user_id="person-a", needs_password=False, role="TEST_EXECUTOR"):
    return {
        "authenticated": True,
        "user": {"id": user_id, "username": user_id, "display_name": "测试人员", "role": role,
                 "kind": "HUMAN", "must_change_password": needs_password},
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
        "generation": 1,
        "can_submit_factory": not needs_password and role in {"TEST_EXECUTOR", "SYSTEM_ADMIN"},
        "can_submit_research": not needs_password and role in {"TEST_EXECUTOR", "SYSTEM_ADMIN"},
        "can_review_factory": not needs_password and role in {"FACTORY_REVIEWER", "TEST_EXECUTOR", "SYSTEM_ADMIN"},
    }


class MockPlatform:
    def __init__(self):
        self.calls = []
        self.current_session = {"authenticated": False, "user": None}
        self.login_session = session()
        self.replies = {}
        self.block_action = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancel_count = 0
        self.closed = False

    def operation(self, action, payload):
        self.calls.append((action, deepcopy(payload)))
        if action == self.block_action:
            self.entered.set()
            self.release.wait(3)
        if action == "login":
            self.current_session = deepcopy(self.login_session)
        elif action == "change_password":
            self.current_session = session()
        elif action == "logout":
            self.current_session = {"authenticated": False, "user": None}
        if action == "factory_preview" and action not in self.replies:
            from rp1_monitor.factory_preview import preview_result_csv

            reply = {"preview": preview_result_csv(payload["path"])}
        else:
            reply = self.replies.get(action, {})
        if isinstance(reply, Exception):
            reply.session = deepcopy(self.current_session)
            raise reply
        return {**deepcopy(reply), "session": deepcopy(self.current_session)}

    def cancel_current(self):
        self.cancel_count += 1
        self.release.set()

    def close(self):
        self.closed = True
        self.release.set()


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    assert predicate(), "native mock operation did not finish"


@pytest.fixture
def ui(app, tmp_path, monkeypatch):
    service = MockPlatform()
    window = PlatformMonitorWindow("http://127.0.0.1:1", platform_service=service,
                                   factory_directory=tmp_path / "factory")
    monkeypatch.setattr(window, "connect_source", lambda: None)
    monkeypatch.setattr(window, "restore_station_connection", lambda *args: None)
    window.show()
    app.processEvents()
    yield window, service
    service.release.set()
    if window.platform_worker is not None:
        window.cancel_platform_task()
        wait_for(app, lambda: window.platform_worker is None)
    window.close()
    wait_for(app, lambda: not window.isVisible())
    window.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def sign_in(app, window, service):
    window.platform_url_edit.setText("https://platform.example:8443")
    window.username_edit.setText("person-a")
    window.password_edit.setText("synthetic-password")
    window.login()
    wait_for(app, lambda: window.platform_worker is None)
    return service


def make_result_csv(window, path, *, sn="MOCK-SN-A", simulated=False):
    store = window.factory_panel.store
    store.set_model_dof(sn, 24)
    changes = []
    for row in store.report_rows(sn):
        fields = {"operator": "MOCK 测试人员", "notes": "simulated：合成UI验收数据" if simulated else "MOCK 合成UI验收资料"}
        if row["row_key"] == "SYS-010":
            fields.update(verdict="不通过", actual_result="MOCK父项原结论，不从子项推导")
        elif row["row_key"] == "SYS-010/walking":
            fields.update(verdict="通过", actual_result="MOCK行走原记录")
        elif row["row_key"] == "SYS-010/running":
            fields.update(verdict="不通过", actual_result="MOCK跑步原记录")
        changes.append({"row_key": row["row_key"], "source_token": row["source_token"], "fields": fields})
    store.save_report_edits(sn, changes, station_id="PC-01")
    exported = store.export_result_summary(sn, path.parent / "exports")
    shutil.copyfile(exported, path)
    path.chmod(0o600)
    return path


def ready_factory(window, path):
    make_result_csv(window, path)
    window._local_exported(str(path))
    wait_for(QApplication.instance(), lambda: window.platform_worker is None)
    assert window._factory_preview is not None
    window.factory_manual_mapping.setChecked(True)
    window.factory_robot.setText("robot-a")
    window.factory_mapping_confirmed.setChecked(True)
    window.factory_kind.setCurrentIndex(window.factory_kind.findData("real"))
    window.factory_reason.setText("已核对原始测试证据")
    window._factory_step_requested(2)
    window.factory_verified.setChecked(True)
    window.factory_completed.setChecked(True)


def test_platform_state_does_not_block_fresh_station_marker_initialization(tmp_path):
    from rp1_monitor.platform_workspace import NativePlatformService
    from rp1_monitor.station import prepare_state

    prior_station = tmp_path / "prior-layout"
    prior_station.mkdir(mode=0o700)
    prior_service = NativePlatformService(prior_station / "platform")
    try:
        with pytest.raises(ValueError, match="拒绝接管"):
            prepare_state(prior_station)
    finally:
        prior_service.close()

    station = tmp_path / "fresh-station"
    state = platform_state_path(station)
    assert state == tmp_path / "fresh-station-platform"
    service = NativePlatformService(state)
    try:
        assert not station.exists()
        assert prepare_state(station) == station.resolve()
        assert (station / "station-owner.json").is_file()
        assert state.parent == station.parent
    finally:
        service.close()


def test_startup_is_full_local_and_platform_login_is_optional(ui):
    window, service = ui
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert window.pages.count() == 10
    assert window.platform_worker is None
    assert service.calls == []
    assert window.research_nav.isEnabled()
    assert not window.factory_submit_button.isEnabled()
    assert window.reader is None
    assert not window.station_url
    assert window.account_label.text() == "未登录（本地可用）"
    assert window.username_edit.isVisible() and window.password_edit.isVisible()
    assert not window.account_dialog.isVisible()
    assert window.platform_mode.currentData() == "LOCAL"
    window.research_nav.click()
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert "顶栏登录" in window.platform_notice.text()
    assert not window.research_nav.isChecked()
    window.open_account()
    assert window.account_dialog.isVisible() and not window.account_dialog.isModal()
    assert window.platform_stack.currentWidget() is window.local_workbench
    window.offline_button.click()
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert not window.account_dialog.isVisible()


def test_login_clears_password_and_emits_account_event_without_local_reinitialization(app, ui):
    window, service = ui
    emitted = []
    window.first_authenticated.connect(lambda: emitted.append(True))
    service.block_action = "login"
    window.platform_url_edit.setText("https://platform.example:8443")
    window.username_edit.setText("person-a")
    window.password_edit.setText("synthetic-password")
    window.login()
    assert window.password_edit.text() == ""
    assert emitted == []
    wait_for(app, service.entered.is_set)
    assert not window.factory_nav.isEnabled()
    service.release.set()
    wait_for(app, lambda: window.platform_worker is None)
    assert emitted == [True]
    assert [name for name, _ in service.calls] == ["configure", "login"]
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert window.factory_nav.isEnabled()


def test_first_password_change_gates_only_platform_and_preserves_local_workbench(app, ui):
    window, service = ui
    service.login_session = session(needs_password=True)
    emitted = []
    window.first_authenticated.connect(lambda: emitted.append(True))
    sign_in(app, window, service)
    assert window.password_box.isVisible()
    assert window.factory_nav.isEnabled() and window.research_nav.isEnabled()
    assert not window.factory_submit_button.isEnabled()
    assert window.platform_stack.currentWidget() is window.local_workbench
    window.open_local_workbench(9)
    assert window.pages.currentIndex() == 9
    assert window.factory_panel.isEnabled()
    window.open_account()
    assert emitted == []
    window.current_password.setText("synthetic-old")
    window.new_password.setText("synthetic-new")
    window.confirm_password.setText("synthetic-new")
    window.change_password()
    assert window.current_password.text() == window.new_password.text() == window.confirm_password.text() == ""
    wait_for(app, lambda: window.platform_worker is None)
    assert emitted == [True]
    assert window._valid_session()


def test_logout_and_expiry_do_not_stop_local_source_or_recording(app, ui, monkeypatch):
    window, service = ui
    sign_in(app, window, service)
    stopped = []
    monkeypatch.setattr(window, "disconnect_source", lambda: stopped.append("source"))
    monkeypatch.setattr(window.recordings_panel, "stop_recording", lambda: stopped.append("recording"))
    window.open_local_workbench(8)
    window.logout()
    wait_for(app, lambda: window.platform_worker is None)
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert window.pages.currentIndex() == 8
    assert window.joint_inspection_panel.isEnabled()
    assert stopped == []
    assert not window._valid_session()
    window.open_local_workbench()
    assert window.platform_stack.currentWidget() is window.local_workbench
    expired = session()
    expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    window._set_session(expired)
    window._check_session_expiry()
    assert not window._valid_session()
    assert stopped == []
    assert "到期" in window.platform_notice.text()
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert window.pages.currentIndex() == 8
    assert window.joint_inspection_panel.isEnabled()


def test_unauthenticated_original_tools_and_local_save_remain_available(app, ui, tmp_path, monkeypatch):
    window, service = ui
    monkeypatch.setattr(window.recordings_panel, "refresh_context", lambda: None)
    for index in range(10):
        assert window.nav.button(index).isEnabled()
        window.nav.button(index).click()
        app.processEvents()
        assert window.pages.currentIndex() == index
        assert window.platform_stack.currentWidget() is window.local_workbench
        assert window.pages.currentWidget().isEnabled()
    path = make_result_csv(window, tmp_path / "local-only.csv")
    window.factory_panel.result_summary.exported.emit(str(path))
    assert path.is_file() and "MOCK-SN-A" in path.read_text()
    assert service.calls == []
    assert window._factory_preview is None
    assert window.platform_stack.currentWidget() is window.local_workbench


@pytest.mark.parametrize("action", ["setup", "contexts", "factory_preview", "factory_results", "factory_detail", "factory_submit", "factory_review", "research_records", "research_export", "research_submit", "research_results", "research_detail", "tasks", "retry"])
def test_unauthenticated_central_operations_never_start_a_worker_or_service(ui, action):
    window, service = ui
    assert not window.request(action, {})
    assert window.platform_worker is None and service.calls == []
    assert window.platform_stack.currentWidget() is window.local_workbench


def test_original_local_initialization_runs_once_across_platform_account_changes(app, ui, monkeypatch):
    window, service = ui
    connections = []
    monkeypatch.setattr(window, "connect_source", lambda: connections.append("local-connect"))
    args = SimpleNamespace(factory=False, quick_connect=False)
    window.start_initial_connection(args)
    window.start_initial_connection(args)
    assert connections == ["local-connect"] and service.calls == []
    sign_in(app, window, service)
    window.request("profile", {}, window._profile_loaded)
    wait_for(app, lambda: window.platform_worker is None)
    window.logout()
    wait_for(app, lambda: window.platform_worker is None)
    service.login_session = session(needs_password=True)
    sign_in(app, window, service)
    window.current_password.setText("old")
    window.new_password.setText("new")
    window.confirm_password.setText("new")
    window.change_password()
    wait_for(app, lambda: window.platform_worker is None)
    window.start_initial_connection(args)
    assert connections == ["local-connect"]


def test_quick_connect_bootstraps_local_backend_once_without_platform_login(app, ui, tmp_path, monkeypatch):
    window, service = ui
    calls, restored = [], []
    def ensure(directory, port):
        calls.append((directory, port))
        return "http://127.0.0.1:19999", tmp_path / "mock-token"
    monkeypatch.setattr("rp1_monitor.station.ensure_station", ensure)
    monkeypatch.setattr(window, "restore_station_connection", lambda: restored.append(True))
    args = SimpleNamespace(factory=False, quick_connect=True, station_dir=tmp_path / "station", station_port=19999)
    window.start_initial_connection(args)
    window.start_initial_connection(args)
    wait_for(app, lambda: window.platform_worker is None and restored)
    assert calls == [(args.station_dir, 19999)] and restored == [True]
    assert service.calls == [] and not window._valid_session()
    sign_in(app, window, service)
    window.start_initial_connection(args)
    assert len(calls) == 1 and restored == [True]


def test_factory_startup_only_opens_local_page_and_never_bootstraps_station(ui, monkeypatch):
    window, service = ui
    def forbidden(*args, **kwargs):
        pytest.fail("factory startup must not connect or start the local station")
    monkeypatch.setattr(window, "connect_source", forbidden)
    monkeypatch.setattr("rp1_monitor.station.ensure_station", forbidden)
    window.start_initial_connection(SimpleNamespace(factory=True, quick_connect=True))
    window.start_initial_connection(SimpleNamespace(factory=True, quick_connect=True))
    assert window.pages.currentIndex() == 9
    assert window.platform_stack.currentWidget() is window.local_workbench
    assert window.platform_worker is None and service.calls == []


def test_platform_service_is_lazy_and_close_without_login_never_creates_state(app, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("local startup/close must not instantiate platform persistence")
    monkeypatch.setattr("rp1_monitor.platform_workspace.NativePlatformService", forbidden)
    state = tmp_path / "platform-not-created"
    window = PlatformMonitorWindow("http://127.0.0.1:1", platform_state_directory=state, factory_directory=tmp_path / "factory")
    assert isinstance(window.platform_service, LazyPlatformService)
    assert not state.exists()
    window.show()
    window.open_local_workbench(9)
    app.processEvents()
    window.close()
    wait_for(app, lambda: not window.isVisible())
    window.finish_platform_workers()
    assert not state.exists()
    window.deleteLater()
    app.processEvents()


def test_lazy_service_cancellation_during_construction_prevents_login(app, tmp_path, monkeypatch):
    created = threading.Event()
    release = threading.Event()
    services = []
    class SlowPlatform(MockPlatform):
        def __init__(self, _state):
            super().__init__()
            services.append(self)
            created.set()
            assert release.wait(3)
    monkeypatch.setattr("rp1_monitor.platform_workspace.NativePlatformService", SlowPlatform)
    window = PlatformMonitorWindow("http://127.0.0.1:1", platform_state_directory=tmp_path / "platform", factory_directory=tmp_path / "factory")
    window.platform_url_edit.setText("https://platform.example:8443")
    window.username_edit.setText("person-a")
    window.password_edit.setText("synthetic")
    window.login()
    wait_for(app, created.is_set)
    window.cancel_platform_task()
    release.set()
    wait_for(app, lambda: window.platform_worker is None)
    assert len(services) == 1 and services[0].calls == []
    assert not window._valid_session()
    window.close()
    window.deleteLater()
    app.processEvents()
    assert services[0].closed


def test_offline_history_reads_local_store_without_platform_or_device_calls(app, ui):
    window, service = ui
    window.factory_panel.store.set_model_dof("LOCAL-SN", 24)
    window.open_offline_history()
    wait_for(app, lambda: window.platform_worker is None)
    assert service.calls == []
    assert window.platform_stack.currentWidget() is window.offline_history
    window.offline_sn.setCurrentIndex(1)
    wait_for(app, lambda: window.platform_worker is None)
    assert service.calls == []
    assert window.offline_table.rowCount() > 0
    assert window.reader is None


def test_factory_export_only_selects_candidate_and_submit_freezes_metadata(app, ui, tmp_path):
    window, service = ui
    sign_in(app, window, service)
    path = make_result_csv(window, tmp_path / "summary.csv")
    window.factory_panel.result_summary.exported.emit(str(path))
    wait_for(app, lambda: window.platform_worker is None)
    assert any(action == "factory_preview" for action, _ in service.calls)
    assert not any(action == "factory_submit" for action, _ in service.calls)
    assert window.factory_path.text() == str(path)
    assert window.factory_source.isReadOnly()
    assert window.factory_source.text() == "PC-01"
    assert window.factory_meta["sn"].text() == "MOCK-SN-A"
    assert window.factory_flow.index == 1
    ready_factory(window, path)
    preview_hash = window._factory_preview["sha256"]
    service.block_action = "factory_submit"
    window.submit_factory()
    wait_for(app, service.entered.is_set)
    assert not window.factory_nav.isEnabled()
    window.factory_robot.setText("robot-other")
    service.release.set()
    wait_for(app, lambda: window.platform_worker is None)
    payload = [data for action, data in service.calls if action == "factory_submit"][0]
    assert payload["robot_id"] == "robot-a"
    assert payload["source_id"] == "PC-01"
    assert payload["expected_sha256"] == preview_hash
    assert payload["verified"] is payload["completed"] is True
    assert payload["session_generation"] == 1
    assert "operator" not in payload and "owner_id" not in payload


def test_factory_requires_human_confirmation_and_metadata_edits_revoke_it(app, ui, tmp_path):
    window, service = ui
    sign_in(app, window, service)
    ready_factory(window, tmp_path / "summary.csv")
    window.factory_robot.setText("robot-b")
    assert not window.factory_verified.isChecked()
    window.submit_factory()
    assert not any(action == "factory_submit" for action, _ in service.calls)
    ready_factory(window, tmp_path / "summary.csv")
    window._source_changed("NEW-SN")
    assert not window.factory_verified.isChecked()
    assert not window.factory_completed.isChecked()
    assert window.factory_path.text() == str(tmp_path / "summary.csv")


def test_viewer_cannot_submit_and_factory_reviewer_does_not_get_upload_permission(app, ui):
    window, service = ui
    service.login_session = session(role="VIEWER")
    sign_in(app, window, service)
    window.select_domain("FACTORY")
    assert not window.factory_submit_button.isEnabled()
    window._set_session(session(role="FACTORY_REVIEWER"))
    assert not window.factory_submit_button.isEnabled()
    assert not window.research_submit_button.isEnabled()
    assert window.workspace_tabs.currentIndex() == 1
    assert not window.workspace_tabs.isTabEnabled(0)


def test_shared_local_tools_do_not_claim_platform_domain_or_change_source_metadata(app, ui):
    window, service = ui
    sign_in(app, window, service)
    window.select_domain("RESEARCH")
    assert window.research_nav.isChecked()
    window.open_local_workbench(9)
    assert window.local_nav.isChecked()
    assert not window.research_nav.isChecked()
    assert not window.factory_nav.isChecked()
    assert window.domain == "RESEARCH"
    assert window.pages.currentIndex() == 9
    assert not any(action in {"factory_submit", "research_submit"} for action, _ in service.calls)


def test_research_record_reference_never_defaults_declarations_or_pass(app, ui):
    window, service = ui
    sign_in(app, window, service)
    service.replies["research_records"] = {"items": [{"id": "record-a", "recording_id": "record-a", "robot_id": "robot-a", "source_kind": "ros2", "run_number": "run-a", "started_at": "2026-11-01T00:00:00Z", "ended_at": "2026-11-02T00:00:00Z", "frequency_hz": 1, "sample_count": 25}]}
    window.select_domain("RESEARCH")
    window.pg_file.setText("/synthetic/postgres-url.txt")
    window.load_research_records()
    wait_for(app, lambda: window.platform_worker is None)
    window.research_record.setCurrentIndex(1)
    assert "2026-11-01" in window.record_reference.text()
    assert window.research_seconds.text() == ""
    assert window.research_cycles.text() == ""
    assert window.research_exceptions.text() == ""
    assert window.research_started.text() == window.research_ended.text() == ""
    assert window.research_verdict.currentData() == ""
    assert not window.research_verified.isChecked()


def test_research_uses_exact_export_and_real_context_not_factory_csv(app, ui):
    window, service = ui
    sign_in(app, window, service)
    context = {name: f"00000000-0000-4000-8000-{index:012d}" for index, name in enumerate(("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id"), 1)}
    window.select_domain("RESEARCH")
    window._exports = {"export_id": "exact-export-a", "path": "/synthetic/raw.csv"}
    window.research_context.addItem("真实计划上下文", {"context": context})
    window.research_context.setCurrentIndex(window.research_context.count() - 1)
    window.research_station.addItem("工位", {"id": context["station_id"]})
    window.research_station.setCurrentIndex(window.research_station.count() - 1)
    window.research_test_name.setText("实际研发测试")
    window.research_object.setText("实际样品")
    window.research_tester.setText("实际测试人")
    window.research_started.setText("2026-11-01T08:00:00+08:00")
    window.research_ended.setText("2026-11-01T09:00:00+08:00")
    window.research_seconds.setText("2000")
    window.research_cycles.setText("0")
    window.research_exceptions.setText("2")
    window.research_verdict.setCurrentIndex(window.research_verdict.findData("failed"))
    window.research_notes.setPlainText("已核对实际测试与原监控证据的关系")
    window.research_reason.setText("人员核实后送审")
    window.research_verified.setChecked(True)
    window.research_completed.setChecked(True)
    window.submit_research()
    wait_for(app, lambda: window.platform_worker is None)
    payload = [data for action, data in service.calls if action == "research_submit"][0]
    assert payload["context"] == context
    assert payload["export_id"] == "exact-export-a"
    assert payload["verification"]["declared_active_seconds"] == 2000
    assert payload["verification"]["verdict"] == "failed"
    assert "path" not in payload
    assert not any(action == "factory_submit" for action, _ in service.calls)


def test_catalog_preserves_older_published_context_and_non_editor_plan(app, ui):
    window, service = ui
    sign_in(app, window, service)
    context = {name: f"00000000-0000-4000-8000-{index:012d}" for index, name in enumerate(("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id"), 1)}
    window._setup_loaded({"catalog": [{"id": "case", "code": "REL-SYS-001", "name": "实际已发布用例", "asset_kind": "WHOLE_MACHINE", "target_part_code": "SYS", "enabled": True, "version_id": "latest-v2-uuid", "version": 2}, {"id": "draft", "code": "draft", "asset_kind": "WHOLE_MACHINE", "target_part_code": "SYS"}], "campaigns": [{"id": "existing-plan", "code": "PLAN-A", "name": "已有计划", "asset_kind": "WHOLE_MACHINE", "status": "ACTIVE", "can_edit": False}], "options": {"stations": []}})
    assert window.research_catalog.count() == 2
    assert window.research_campaign.count() == 2
    window._contexts = [{"id": "actual", "case_code": "REL-SYS-001", "version": 1, "test_case_version_id": context["test_case_version_id"], "context": context}, {"id": "wrong", "case_code": "REL-SYS-OTHER", "context": context}]
    window.research_catalog.setCurrentIndex(1)
    assert window.research_context.count() == 2
    assert window.research_context.itemData(1)["id"] == "actual"
    assert "v1" in window.research_context.itemText(1)
    assert window.research_context.itemData(1)["context"] == context


def test_single_worker_cancel_waits_for_finish_and_does_not_show_late_data(app, ui):
    window, service = ui
    sign_in(app, window, service)
    window.select_domain("FACTORY")
    service.block_action = "factory_results"
    service.replies["factory_results"] = {"items": [{"id": "late", "filename": "late.csv", "status": "PENDING"}]}
    window.refresh_results()
    wait_for(app, service.entered.is_set)
    worker = window.platform_worker
    assert not window.request("tasks", {})
    assert window.platform_worker is worker
    window.cancel_platform_task()
    wait_for(app, lambda: window.platform_worker is None)
    assert service.cancel_count >= 1
    assert window.results_table.rowCount() == 0
    wait_for(app, lambda: not _WORKERS)
    assert "取消" in window.platform_notice.text()


def test_identity_expiry_discards_late_old_account_response(app, ui):
    window, service = ui
    sign_in(app, window, service)
    window.select_domain("FACTORY")
    service.block_action = "factory_results"
    service.replies["factory_results"] = {"items": [{"id": "private-old", "filename": "old.csv", "status": "APPROVED"}]}
    window.refresh_results()
    wait_for(app, service.entered.is_set)
    window.session["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    window._check_session_expiry()
    wait_for(app, lambda: window.platform_worker is None)
    assert not window._valid_session()
    assert window.results_table.rowCount() == 0
    assert "到期" in window.platform_notice.text()


def test_session_failure_and_role_downgrade_clear_private_cached_results(app, ui):
    window, service = ui
    sign_in(app, window, service)
    window.select_domain("FACTORY")
    window._results_loaded({"items": [{"id": "private", "filename": "pending.csv", "status": "PENDING"}]})
    window._exports = {"export_id": "private-export"}
    service.current_session = session(role="VIEWER")
    window.request("profile", {}, window._profile_loaded)
    wait_for(app, lambda: window.platform_worker is None)
    assert window.results_table.rowCount() == 0
    assert window._exports is None
    assert not window.factory_submit_button.isEnabled()

    class SessionFailure(Exception):
        status = 401
        message = "平台会话已失效"

    service.current_session = {"authenticated": False, "user": None}
    service.replies["factory_results"] = SessionFailure()
    window.refresh_results()
    wait_for(app, lambda: window.platform_worker is None)
    assert not window._valid_session()
    assert "会话已失效" in window.platform_notice.text()


def test_simulated_factory_results_are_visible_and_never_offer_approval(app, ui):
    window, service = ui
    sign_in(app, window, service)
    window.select_domain("FACTORY")
    record = {"id": "simulated", "filename": "simulated.csv", "source_kind": "simulated", "status": "PENDING", "revision": 1, "can_review": True}
    window._results_loaded({"items": [record]})
    assert "模拟数据" in window.results_table.item(0, 1).text()
    window._detail_loaded({"archive": record})
    dialog = next(iter(window._detail_dialogs))
    titles = [widget.text() for widget in dialog.findChildren(type(window.factory_submit_button))]
    assert "批准工厂汇总" not in titles
    assert "驳回工厂汇总" in titles
    assert not window.browser_curve_button.isVisible()


def test_close_cancels_and_finishes_platform_worker_before_local_store_close(app, ui, monkeypatch):
    window, service = ui
    sign_in(app, window, service)
    service.block_action = "factory_results"
    window.select_domain("FACTORY")
    window.refresh_results()
    wait_for(app, service.entered.is_set)
    observed = []
    original_close = window.factory_panel.store.close

    def close_store():
        observed.append(window.platform_worker is None or not window.platform_worker.isRunning())
        original_close()

    monkeypatch.setattr(window.factory_panel.store, "close", close_store)
    window.close()
    wait_for(app, lambda: not window.isVisible())
    assert service.cancel_count >= 1
    assert service.closed
    assert observed == [True]


@pytest.mark.parametrize("base", ["http://platform.example", "https://person:password@platform.example", "https://platform.example?token=secret"])
def test_browser_links_reject_insecure_or_credential_bearing_base(base):
    with pytest.raises(ValueError):
        platform_link(base)


def test_browser_links_use_business_identity_without_native_credentials():
    factory = parse_qs(urlsplit(platform_link("https://platform.example:8443", "FACTORY", "archive-a")).query)
    research = parse_qs(urlsplit(platform_link("https://platform.example:8443", "RESEARCH", "archive-b", True)).query)
    assert factory == {"view": ["factory"], "kind": ["whole"], "domain": ["FACTORY"], "factory_archive": ["archive-a"]}
    assert research == {"view": ["curves"], "kind": ["whole"], "domain": ["RESEARCH"], "id": ["archive-b"]}
    assert not any("token" in key or "session" in key for key in factory | research)


def test_capture_explicit_mock_native_screens(app, ui, tmp_path):
    from pathlib import Path

    window, service = ui
    sign_in(app, window, service)
    path = make_result_csv(window, tmp_path / "mock-results.csv", simulated=True)
    window._local_exported(str(path))
    wait_for(app, lambda: window.platform_worker is None)
    output = Path("/tmp/rp1-native-workflow-v2-20261106")
    output.mkdir(exist_ok=True)

    def capture(name, primary):
        window.platform_notice.setText("MOCK 新版工作流验收 · 合成资料 · 未连接任何设备或真实平台")
        window.resize(1366, 768)
        for _ in range(5):
            app.processEvents()
        assert (window.width(), window.height()) == (1366, 768)
        point = primary.mapTo(window, QPoint(0, 0))
        assert primary.isVisible() and window.rect().contains(point)
        assert window.rect().contains(primary.mapTo(window, primary.rect().bottomRight()))
        assert window.grab().save(str(output / (name + "-mock.png")))

    window.factory_flow.set_index(1)
    index = next(i for i, row in enumerate(window._factory_projects) if row["case_code"] == "SYS-010")
    window.factory_project_list.setCurrentCell(index, 0)
    window.factory_rows_table.setCurrentCell(2, 0)
    capture("factory-step2", window.factory_flow.next)
    window.factory_manual_mapping.setChecked(True)
    window.factory_robot.setText("MOCK-ROBOT-A")
    window.factory_mapping_confirmed.setChecked(True)
    window.factory_flow.set_index(0)
    capture("factory-step1", window.factory_flow.next)
    window._factory_step_requested(2)
    capture("factory-step3", window.factory_submit_button)
    window.select_domain("RESEARCH")
    for step in range(3):
        window.research_flow.set_index(step)
        capture("research-step" + str(step + 1), window.research_submit_button if step == 2 else window.research_flow.next)
    assert window.research_start_picker.input.dateTime() == window.research_start_picker.input.minimumDateTime()
    assert window.research_end_picker.input.dateTime() == window.research_end_picker.input.minimumDateTime()
    assert not any(field.text() for field in window.duration_fields)
    assert window.research_verdict.currentData() == ""


def test_factory_preview_needs_no_mapping_but_submission_does(app, ui, tmp_path):
    window, service = ui
    sign_in(app, window, service)
    path = make_result_csv(window, tmp_path / 'preview.csv')
    window._local_exported(str(path))
    wait_for(app, lambda: window.platform_worker is None)
    assert window.factory_flow.index == 1
    assert not window.factory_robot.text()
    assert window.factory_kind.currentData() == ''
    window._factory_step_requested(1)
    assert window.factory_flow.index == 1
    window._factory_step_requested(2)
    assert window.factory_flow.index == 1
    window.submit_factory()
    assert not any(action == 'factory_submit' for action, _ in service.calls)


def test_csv_sn_binding_does_not_reuse_current_local_sn_or_infer_real(app, ui, tmp_path, monkeypatch):
    window, service = ui
    sign_in(app, window, service)
    window.factory_panel.sn = 'LOCAL-SN-A'
    looked_up = []

    def binding(sn):
        looked_up.append(sn)
        return {'robot_id': 'wrong-robot-a', 'source_kind': 'ros2'} if sn == 'LOCAL-SN-A' else None

    monkeypatch.setattr(window.factory_panel.store, 'sn_identity', binding)
    path = make_result_csv(window, tmp_path / 'external.csv', sn='CSV-SN-B')
    window._local_exported(str(path))
    wait_for(app, lambda: window.platform_worker is None)
    assert 'CSV-SN-B' in looked_up
    assert not window.factory_robot.text()
    assert window.factory_kind.currentData() == ''
    monkeypatch.setattr(window.factory_panel.store, 'sn_identity', lambda sn: {'robot_id': 'known-robot-b', 'source_kind': 'ros2'} if sn == 'CSV-SN-B' else None)
    window._prefill_factory_identity()
    assert window.factory_robot.text() == 'known-robot-b'
    assert window.factory_kind.currentData() == ''
    assert not window.factory_mapping_confirmed.isChecked()


def test_parent_child_missing_and_raw_content_remain_distinct(app, ui, tmp_path):
    window, service = ui
    sign_in(app, window, service)
    path = make_result_csv(window, tmp_path / 'partial.csv')
    rows = list(csv.reader(io.StringIO(path.read_text(encoding='utf-8-sig'), newline='')))
    kept = [rows[0]]
    for row in rows[1:]:
        if row[7] == 'SYS-010' or row[7] == 'SYS-001' and row[6] == '子项':
            if row[7] == 'SYS-001':
                row[9] = '<b>CSV人工测试名称</b>'
                row[13] = '<script>原始实测文本</script>'
            kept.append(row)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        csv.writer(handle).writerows(kept)
    window._local_exported(str(path))
    wait_for(app, lambda: window.platform_worker is None)
    positions = {item['case_code']: index for index, item in enumerate(window._factory_projects)}
    assert window.factory_project_list.item(positions['SYS-001'], 2).text() == '仅子项/父项缺失'
    assert window.factory_project_list.item(positions['SYS-002'], 2).text() == '本文件未包含'
    assert window.factory_project_list.item(positions['SYS-010'], 2).text() == '不通过'
    window.factory_project_list.setCurrentCell(positions['SYS-001'], 0)
    window.factory_rows_table.setCurrentCell(0, 0)
    assert '<b>CSV人工测试名称</b>' in window.factory_row_caption.text()
    assert window.factory_row_caption.textFormat() == Qt.TextFormat.PlainText
    assert window.factory_result_text.toPlainText() == '<script>原始实测文本</script>'
    assert window.factory_record_text.toPlainText()
    window.factory_project_list.setCurrentCell(positions['SYS-002'], 0)
    assert all(not widget.toPlainText() for widget in (window.factory_result_text, window.factory_criterion_text, window.factory_notes_text, window.factory_record_text))


def test_account_change_clears_all_factory_preview_displays_and_resets_step(app, ui, tmp_path):
    window, service = ui
    sign_in(app, window, service)
    ready_factory(window, tmp_path / 'private-person-a.csv')
    assert window.factory_flow.index == 2
    assert window.factory_hash_label.text()
    assert 'MOCK-SN-A' in window.factory_confirmation_summary.text()
    window._set_session(session(user_id='person-b'))
    assert window._factory_preview is None
    assert not window.factory_path.text()
    assert not window.factory_robot.text()
    assert window.factory_flow.index == 0
    assert 'private-person-a' not in window.factory_file_label.text()
    assert 'MOCK-SN-A' not in window.factory_confirmation_summary.text()
    assert 'SHA256' not in window.factory_hash_label.text()
    assert window._factory_current_project is None
    assert all(not widget.toPlainText() for widget in (window.factory_result_text, window.factory_criterion_text, window.factory_notes_text, window.factory_record_text))


def test_visible_date_duration_and_canonical_clear_together_on_record_and_account_change(app, ui):
    window, service = ui
    sign_in(app, window, service)

    def fill():
        instant = QDateTime.fromString('2026-11-06T10:00:00.123+08:00', Qt.DateFormat.ISODateWithMs)
        window.research_start_picker.input.setDateTime(instant)
        window.research_end_picker.input.setDateTime(instant.addSecs(7200))
        for widget, value in zip(window.duration_fields, ('1', '2', '1.5'), strict=True):
            widget.setText(value)
        assert float(window.research_seconds.text()) == 3721.5
        assert '.123' in window.research_started.text()
        assert window.research_started.text().endswith('+08:00')

    def cleared():
        assert window.research_start_picker.input.dateTime() == window.research_start_picker.input.minimumDateTime()
        assert window.research_end_picker.input.dateTime() == window.research_end_picker.input.minimumDateTime()
        assert not any(widget.text() for widget in window.duration_fields)
        assert not any(widget.text() for widget in (window.research_started, window.research_ended, window.research_seconds))

    fill()
    window._recording_selected()
    cleared()
    fill()
    window.export_evidence_label.setText('person-a-private-path')
    window.record_reference.setText('person-a-private-record')
    window.research_flow.set_index(2)
    window._set_session(session(user_id='person-b'))
    cleared()
    assert window.research_flow.index == 0
    assert 'person-a' not in window.export_evidence_label.text()
    assert 'person-a' not in window.record_reference.text()


def test_friendly_duration_supports_fractional_seconds_and_rejects_invalid_values(app, ui):
    window, service = ui
    sign_in(app, window, service)
    seconds = window.duration_fields[2]
    for value in ('1.5', '0.001', '59.999', '0'):
        seconds.setText(value)
        assert float(window.research_seconds.text()) == float(value)
    for value in ('NaN', 'Inf', '-1', '60', '1e999'):
        seconds.setText(value)
        assert window.research_seconds.text() == ''
    seconds.clear()
    window.duration_fields[0].setText('1.5')
    assert window.research_seconds.text() == ''
    window.duration_fields[0].clear()
    window.duration_fields[1].setText('1.5')
    assert window.research_seconds.text() == ''
    window.duration_fields[1].clear()
    assert window.research_seconds.text() == ''


def test_success_response_with_expired_or_downgraded_session_never_refills_private_results(app, ui):
    window, service = ui
    sign_in(app, window, service)
    for response_session in ({'authenticated': False, 'user': None}, session(role='VIEWER')):
        window._set_session(session())
        window.select_domain('FACTORY')
        service.current_session = response_session
        service.replies['factory_results'] = {'items': [{'id': 'private-old', 'filename': 'old.csv', 'status': 'PENDING'}]}
        epoch = window._identity_epoch
        window.refresh_results()
        wait_for(app, lambda: window.platform_worker is None)
        assert window.results_table.rowCount() == 0
        assert window._identity_epoch == epoch  # No Qt expiry tick was involved.
        assert '会话或权限已变化' in window.platform_notice.text()


def test_expanded_advanced_settings_keep_wizard_footer_inside_1366(app, ui, tmp_path):
    from rp1_monitor.desktop_platform import Disclosure

    window, service = ui
    sign_in(app, window, service)
    ready_factory(window, tmp_path / 'expand.csv')
    window.factory_flow.set_index(0)
    for item in window.factory_flow.findChildren(Disclosure):
        item.toggle.setChecked(True)
    window.factory_manual_mapping.setChecked(True)

    def within(primary):
        window.resize(1366, 768)
        for _ in range(5):
            app.processEvents()
        assert (window.width(), window.height()) == (1366, 768)
        assert primary.isVisible()
        assert window.rect().contains(primary.mapTo(window, primary.rect().bottomRight()))

    within(window.factory_flow.next)
    window.select_domain('RESEARCH')
    window.research_flow.set_index(0)
    for item in window.research_flow.findChildren(Disclosure):
        item.toggle.setChecked(True)
    within(window.research_flow.next)
    assert window.research_flow.body.verticalScrollBar().maximum() > 0


def test_detail_dialogs_use_structured_widgets_with_collapsed_proof_and_history(app, ui, tmp_path):
    from rp1_monitor.desktop_platform import Disclosure
    from rp1_monitor.desktop_platform_details import FactoryArchiveView
    from rp1_monitor.desktop_research_archive_view import ResearchArchiveView
    from rp1_monitor.factory_preview import preview_result_csv

    window, service = ui
    sign_in(app, window, service)
    preview = preview_result_csv(str(make_result_csv(window, tmp_path / 'detail.csv')))
    window.select_domain('FACTORY')
    item = {'id': 'factory-a', 'filename': 'detail.csv', 'sha256': preview['sha256'], 'serial_number': preview['serial_number'], 'status': 'PENDING', 'revision': 1, 'can_review': True, 'columns': preview['columns'], 'rows': [{'record_id': row['record_id'], 'version': 1, 'values': row['values']} for row in preview['rows']], 'review_history': []}
    window._detail_loaded({'archive': item})
    dialog = next(iter(window._detail_dialogs))
    app.processEvents()
    assert dialog.findChild(FactoryArchiveView)
    assert all(not group.body.isVisible() for group in dialog.findChildren(Disclosure))
    dialog.reject()
    app.processEvents()
    window.select_domain('RESEARCH')
    window._detail_loaded({'archive': {'id': 'research-a', 'filename': 'research.csv', 'actual_object': 'MOCK样品', 'submission_status': 'PENDING', 'revision': 1, 'facts': {'active_seconds': None}, 'current_verification': {'declared_active_seconds': 1.5}, 'review_history': []}})
    dialog = next(iter(window._detail_dialogs))
    app.processEvents()
    assert dialog.findChild(ResearchArchiveView)
    assert all(not group.body.isVisible() for group in dialog.findChildren(Disclosure))
    assert (dialog.width(), dialog.height()) == (1050, 680)
