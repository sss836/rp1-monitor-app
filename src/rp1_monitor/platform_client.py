"""Memory-only human platform session for native UI workers, independent of Qt."""

import json
from datetime import datetime, timezone
from uuid import UUID

from .factory_bridge import MAX_RESPONSE_BUDGET_BYTES, BridgeError, JsonHttpClient, timestamp


class NativePlatformError(BridgeError):
    def __init__(self, code, status=None):
        super().__init__(code, status)
        messages = {
            "cancelled": "操作已取消，已冻结的待传任务仍保留。",
            "login_required": "请先登录平台。",
            "password_change_required": "首次登录必须先修改密码。",
            "invalid_credentials": "账号、当前密码或登录信息不正确。",
            "csrf_invalid": "平台会话校验失败，请重新登录。",
            "forbidden": "当前账号没有此操作权限。",
            "factory_scope_required": "当前工厂账号不能访问研发分区。",
            "request_deadline_exceeded": "平台请求超时；未确认的任务仍保留，请核对后重试。",
            "network_error": "平台网络不可达，未确认的任务仍保留。",
            "archive_context_conflict": "该原始文件已关联其他用例、资产或测试上下文，请先在平台核对原归档；本次未修改旧归类。",
            "archive_verification_conflict": "该原始文件已有不同的人工核实版本，请在平台核对后处理；本次声明未被当成已接收。",
            "archive_identity_or_owner_mismatch": "平台回执身份与冻结任务不一致，任务已保留，请核对原归档。",
            "verification_station_context_mismatch": "人工核实工位必须与本次冻结的执行上下文工位一致。",
            "export_evidence_changed": "导出证据文件已变化，停止上传；请重新读取原始记录并核实。",
            "research_outbox_full": "研发待传任务达到磁盘预算；原件保留，请先处理已有任务。",
            "recording_export_directory_full": "导出证据目录达到磁盘预算；未删除已有证据，请检查存储空间。",
            "recording_export_disk_reserve": "磁盘可用空间不足预留预算，已停止导出并移除未完成副本。",
            "recording_not_ended": "只能导出已经封口的PostgreSQL记录；此状态不代表硬件测试完成。",
            "factory_csv_changed_since_preview": "结果CSV与核实预览时的SHA256不同；未创建上传任务，请重新预览核实。",
            "csv_file_required": "请选择原17列整机结果CSV文件，文件名须以.csv结尾。",
            "invalid_17_column_csv": "结果文件须保持原17列顺序，最多1000行数据，每字段不超过65536字符；未上传。",
            "invalid_utf8_csv": "无法按UTF-8读取原CSV，或CSV引号格式无效；请重新用原导出功能生成文件。",
            "csv_single_sn_required": "一个结果CSV须对应一个非空、明确且一致的样品SN；未上传。",
            "csv_single_model_required": "一个结果CSV须对应同一实际24轴或28轴机型；未上传。",
            "csv_source_or_record_identity_mismatch": "CSV各行上位机编号须一致，记录编号不得为空；请核对原文件。",
            "csv_record_identity_invalid_or_duplicate": "原CSV记录编号为空、无效或重复；不得把不同结果合并成同一记录编号。",
            "csv_factory_project_mapping_invalid": "CSV项目、层级或结论与当前SYS-001至SYS-012及原子项定义不符；请核对原文件。",
            "invalid_source_id": "CSV上位机编号格式无效，请核对原工厂导出文件中的上位机编号；不得填机器人SN或路径。",
            "input_file_unavailable": "无法读取所选本地文件，请检查文件是否存在及当前用户读取权限，再重新预览。",
            "invalid_input_file": "所选文件不是允许大小的普通文件；工厂结果CSV最多4MiB，请重新选择原工厂导出文件。",
            "input_file_too_large": "所选本地文件超出读取预算；工厂结果CSV最多4MiB，请重新选择原导出文件。",
        }
        self.message = messages.get(code, f"操作未完成（{code}），请核对平台回执或本地配置。")


class PlatformSessionClient(JsonHttpClient):
    def __init__(self, url, *, ca_file=None, allow_http=False, timeout=15):
        super().__init__(url, ca_file=ca_file, allow_http=allow_http, timeout=timeout,
                         response_bytes=MAX_RESPONSE_BUDGET_BYTES)
        self._ca_file, self._allow_http = ca_file, allow_http
        self.user = None
        self.expires_at = None
        self.generation = 0
        self.cancel_event = None

    def check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise NativePlatformError("cancelled")

    def clear(self):
        self.cookies.clear()
        self.user, self.expires_at = None, None
        self.generation += 1

    def csrf(self):
        values = [cookie.value for cookie in self.cookies if cookie.name == "rp1_csrf"]
        return values[0] if len(values) == 1 else ""

    def session(self):
        expires = timestamp(self.expires_at)
        if self.user and expires and expires <= datetime.now(timezone.utc):
            self.clear()
        role = self.user.get("role") if self.user else None
        ready = bool(self.user and not self.user.get("must_change_password"))
        return {
            "authenticated": bool(self.user), "user": dict(self.user) if self.user else None,
            "expires_at": self.expires_at, "generation": self.generation,
            "can_submit_factory": ready and role in {"SYSTEM_ADMIN", "TEST_EXECUTOR"},
            "can_submit_research": ready and role in {"SYSTEM_ADMIN", "TEST_EXECUTOR"},
            "can_review_factory": ready and role in {"SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"},
        }

    def display_session(self, directory):
        if not self.session()["can_submit_research"] or not self.csrf():
            return None
        from .display_presence import DisplaySession

        return DisplaySession(self, directory, ca_file=self._ca_file, allow_http=self._allow_http)

    def call(self, method, path, *, payload=None, body=None, key=None, public=False):
        self.check_cancel()
        if not public and not self.session()["authenticated"]:
            raise NativePlatformError("login_required", 401)
        headers = {}
        if method not in {"GET", "HEAD"}:
            if not public:
                if not self.csrf():
                    raise NativePlatformError("csrf_invalid", 403)
                headers["X-CSRF-Token"] = self.csrf()
            if payload is not None:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
                headers["Content-Type"] = "application/json"
            elif body is not None:
                headers["Content-Type"] = "application/octet-stream"
            if key:
                headers["Idempotency-Key"] = key
        try:
            response = self.request(method, path, body=body, headers=headers)
        except BridgeError as exc:
            self.check_cancel()
            if exc.status == 401 and exc.code != "invalid_credentials":
                self.clear()
            raise NativePlatformError(exc.code, exc.status) from None
        if response == {} and method == "POST" and path == "/api/v1/auth/logout":
            return {}
        if "data" not in response:
            raise NativePlatformError("invalid_platform_envelope")
        return response["data"]

    def _adopt(self, response):
        user = response.get("user") if isinstance(response, dict) else None
        if not isinstance(user, dict) or not user.get("id") or user.get("kind") != "HUMAN":
            self.clear()
            raise NativePlatformError("invalid_login_response")
        try:
            UUID(str(user["id"]))
        except (ValueError, TypeError, AttributeError):
            self.clear()
            raise NativePlatformError("invalid_login_response") from None
        if timestamp(response.get("expires_at")) is None:
            self.clear()
            raise NativePlatformError("invalid_login_response")
        if not self.csrf() or not any(c.name == "rp1_session" for c in self.cookies):
            self.clear()
            raise NativePlatformError("invalid_login_response")
        self.user, self.expires_at = dict(user), response.get("expires_at")
        self.generation += 1
        return self.session()

    def login(self, username, password):
        self.clear()
        return self._adopt(self.call(
            "POST", "/api/v1/auth/login", payload={"username": username, "password": password}, public=True,
        ))

    def current_me(self):
        return self._adopt(self.call("GET", "/api/v1/auth/me"))

    def change_password(self, current_password, new_password):
        self.call("POST", "/api/v1/auth/change-password", payload={
            "current_password": current_password, "new_password": new_password,
        })
        return self.current_me()

    def logout(self):
        try:
            self.call("POST", "/api/v1/auth/logout")
        finally:
            self.clear()

    def post(self, path, body, content_type, *, idempotency_key=None):
        """Compatibility used by the original factory uploader/receipt verifier."""
        self.check_cancel()
        if not self.session()["can_submit_factory"]:
            raise NativePlatformError("forbidden", 403)
        headers = {"Content-Type": content_type, "X-CSRF-Token": self.csrf()}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self.request("POST", path, body=body, headers=headers)
        except BridgeError as exc:
            self.check_cancel()
            if exc.status == 401:
                self.clear()
            raise NativePlatformError(exc.code, exc.status) from None
        if not isinstance(response.get("data"), dict):
            raise NativePlatformError("invalid_platform_envelope")
        return response["data"]

    def upload_result(self, metadata, raw_bytes, idempotency_key):
        from .factory_bridge import PlatformClient
        return PlatformClient.upload_result(self, metadata, raw_bytes, idempotency_key)

    def upload_trace(self, archive_id, raw_bytes, digest):
        from .factory_bridge import PlatformClient
        return PlatformClient.upload_trace(self, archive_id, raw_bytes, digest)
