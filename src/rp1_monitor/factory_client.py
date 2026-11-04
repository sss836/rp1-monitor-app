"""PC client for local recording segments; no motion, central upload or deletion API."""

import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from .factory_models import FactorySegmentInfo, FactorySegmentManifest, FactorySegmentStart
from .models import RobotId, SourceKind

MAX_PACKAGE_BYTES = 2 * 1024**3
MAX_JSON_BYTES = 2 * 1024**2
SEGMENTS_PATH = "/api/v1/factory/segments"
_robot_id = TypeAdapter(RobotId)
_source_kind = TypeAdapter(SourceKind)


class FactoryClientError(ValueError):
    """An actionable local-service error, without transport credentials or raw bodies."""

    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def _local_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("工厂采集地址必须是无凭据、无路径或查询参数的本机回环HTTP地址")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}
            and (port is None or 1 <= port <= 65535)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("工厂采集地址必须是无凭据、无路径或查询参数的本机回环HTTP地址")
    return value.rstrip("/")


class FactoryClient:
    """Use the robot observer directly through a tunnel, or the PC relay's fixed proxy.

    SN remains a user label. The first probe binds the actual robot ID and source;
    subsequent responses must match that identity even if a tunnel is replaced.
    Each operation owns its HTTP client, so there is no GUI shutdown dependency.
    """

    def __init__(self, url, *, transport=None, timeout=10):
        self.url = _local_url(url)
        self.robot_id = None
        self.source_kind = None
        self._transport = transport
        self._timeout = timeout
        self._identity_lock = threading.Lock()

    def _client(self):
        return httpx.Client(
            timeout=self._timeout, trust_env=False, follow_redirects=False,
            transport=self._transport,
        )

    @staticmethod
    def _status(response):
        if 200 <= response.status_code < 300:
            return
        # Redirects are errors and are never followed, including loopback redirects.
        message = {
            404: "采集端没有该片段，或尚不支持工厂记录；请核对采集版本",
            409: "片段状态或身份已变化；请刷新并核对当前记录",
            507: "采集端磁盘不足；已有原始日志保留",
        }.get(response.status_code, "工厂采集服务请求未成功；请核对采集端状态")
        status = response.status_code if response.status_code in {400, 404, 409, 422, 507} else 502
        raise FactoryClientError(message, status)

    def _json(self, client, method, path, **kwargs):
        try:
            with client.stream(method, self.url + path, **kwargs) as response:
                self._status(response)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_JSON_BYTES:
                        raise FactoryClientError("工厂采集响应超过2 MiB限制")
                    body.extend(chunk)
            result = json.loads(body)
            if not isinstance(result, dict):
                raise FactoryClientError("工厂采集服务响应必须是JSON对象")
            return result
        except httpx.HTTPError as exc:
            raise FactoryClientError("无法连接工厂采集服务；请检查网线、SSH隧道和采集端", 503) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FactoryClientError("工厂采集服务没有返回有效JSON") from exc

    def _check_identity(self, value):
        try:
            identity = (_robot_id.validate_python(value["robot_id"]),
                        _source_kind.validate_python(value["source_kind"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise FactoryClientError("工厂采集响应缺少有效机器人身份或数据来源") from exc
        with self._identity_lock:
            if self.robot_id is None:
                self.robot_id, self.source_kind = identity
            elif identity != (self.robot_id, self.source_kind):
                raise FactoryClientError("机器人身份或数据来源已改变；请重新核对连接", 409)

    def _probe(self, client):
        state = self._json(client, "GET", "/api/v1/state")
        self._check_identity(state)
        return state

    def probe(self):
        """Return the original state dict (identity is in top-level fields)."""
        with self._client() as client:
            return self._probe(client)

    def _info(self, value, segment_id=None):
        try:
            info = FactorySegmentInfo.model_validate(value).model_dump(mode="json")
        except ValueError as exc:
            raise FactoryClientError("工厂片段信息格式无效") from exc
        self._check_identity(info)
        if segment_id is not None and info["segment_id"] != str(segment_id):
            raise FactoryClientError("采集端返回了其他片段；已有本地证据保留", 409)
        return info

    def list_segments(self):
        with self._client() as client:
            self._probe(client)
            result = self._json(client, "GET", SEGMENTS_PATH)
            try:
                segments = [self._info(info) for info in result["segments"]]
                active = result["active_segment_id"]
                active = str(UUID(str(active))) if active is not None else None
                if active is not None and active not in {info["segment_id"] for info in segments}:
                    raise ValueError("active segment missing")
                fault = result.get("fault", "")
                if not isinstance(fault, str):
                    raise ValueError("invalid fault")
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, FactoryClientError):
                    raise
                raise FactoryClientError("工厂片段列表格式无效") from exc
            return {"active_segment_id": active, "segments": segments, "fault": fault}

    def start(self, request):
        request = FactorySegmentStart.model_validate(request)
        with self._client() as client:
            self._probe(client)
            body = request.model_dump(mode="json")
            self._check_identity(body)
            result = self._info(self._json(client, "POST", SEGMENTS_PATH, json=body), request.segment_id)
            if any(result[key] != value for key, value in body.items()):
                raise FactoryClientError("采集端返回的SN或测试内容与开始请求不一致", 409)
            return result

    def get_segment(self, segment_id):
        identifier = UUID(str(segment_id))
        with self._client() as client:
            self._probe(client)
            return self._info(self._json(client, "GET", f"{SEGMENTS_PATH}/{identifier}"), identifier)

    def seal(self, segment_id):
        """End exactly this recording segment; never stop a robot policy or motor."""
        identifier = UUID(str(segment_id))
        with self._client() as client:
            self._probe(client)
            self._info(self._json(client, "GET", f"{SEGMENTS_PATH}/{identifier}"), identifier)
            result = self._info(self._json(client, "POST", f"{SEGMENTS_PATH}/{identifier}/seal"), identifier)
            if result["state"] == "recording" or result["ended_at"] is None:
                raise FactoryClientError("采集端尚未确认片段结束；请刷新后核对", 409)
            return result

    def _manifest(self, client, identifier):
        try:
            manifest = FactorySegmentManifest.model_validate(
                self._json(client, "GET", f"{SEGMENTS_PATH}/{identifier}/manifest")
            ).model_dump(mode="json")
        except ValueError as exc:
            if isinstance(exc, FactoryClientError):
                raise
            raise FactoryClientError("片段下载清单格式无效") from exc
        self._check_identity(manifest)
        if manifest["segment_id"] != str(identifier):
            raise FactoryClientError("片段下载清单身份不匹配", 409)
        if manifest["state"] == "recording" or manifest["ended_at"] is None:
            raise FactoryClientError("片段仍在记录；请先结束再导入", 409)
        if manifest["bytes"] > MAX_PACKAGE_BYTES:
            raise FactoryClientError("片段超过2 GiB下载限制；原始日志仍在采集端保留")
        return manifest

    def manifest(self, segment_id):
        identifier = UUID(str(segment_id))
        with self._client() as client:
            self._probe(client)
            return self._manifest(client, identifier)

    def stage_segment(self, segment_id, directory):
        """Download to an owned temporary directory; the store verifies SHA256/SQLite.

        Only fixed names are used. No path or filename provided by the server is
        joined to a local directory, and incomplete files are never published.
        """
        identifier = UUID(str(segment_id))
        directory = Path(directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("片段下载工作目录必须是已有的普通目录")
        data_path, manifest_path = directory / "segment.sqlite", directory / "manifest.json"
        with self._client() as client:
            self._probe(client)
            manifest = self._manifest(client, identifier)
            count = 0
            try:
                with client.stream("GET", self.url + f"{SEGMENTS_PATH}/{identifier}/data",
                                   headers={"Accept-Encoding": "identity"}) as response:
                    self._status(response)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise FactoryClientError("片段下载不接受压缩响应")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdecimal() or int(length) != manifest["bytes"]):
                        raise FactoryClientError("片段下载长度与清单不一致")
                    with data_path.open("xb") as stream:
                        os.chmod(data_path, 0o600)
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            count += len(chunk)
                            if count > min(manifest["bytes"], MAX_PACKAGE_BYTES):
                                raise FactoryClientError("片段下载超过清单字节数；未导入不完整日志")
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
            except httpx.HTTPError as exc:
                raise FactoryClientError("片段下载中断；已有证据保留，可重新导入", 503) from exc
            if count != manifest["bytes"]:
                raise FactoryClientError("片段下载不完整；字节数与清单不一致")
            with manifest_path.open("x", encoding="utf-8") as stream:
                os.chmod(manifest_path, 0o600)
                json.dump(manifest, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
        return manifest_path, data_path

    def import_segment(self, segment_id, store, *, tempdir=None):
        """Keep original robot files; atomically publish only after store validation."""
        directory = Path(tempdir) if tempdir is not None else store.directory
        with tempfile.TemporaryDirectory(prefix=".factory-download-", dir=directory) as temporary:
            manifest, data = self.stage_segment(segment_id, temporary)
            return store.import_segment(manifest, data)
