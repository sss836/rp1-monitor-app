"""Pi-style provider registry, credential resolution and wire-protocol adapters.

Configuration is server-owned. Requests select registered providers, never URLs.
"""

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(default="server", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    model: str = Field(default="", max_length=128, pattern=r"^[^\r\n]*$")
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        key = value.get_secret_value().strip()
        if not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key):
            raise ValueError("API Key 格式无效")
        return SecretStr(key)


class ModelError(ValueError):
    """Only safe, user-facing messages; never include upstream bodies or keys."""


@dataclass
class AISettings:
    enabled: bool = False
    endpoint: str = "https://api.openai.com/v1/responses"
    model: str = ""
    api_key: str = field(default="", repr=False)
    provider: str = "server"
    api: str = "openai-responses"
    compat: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("RP1_AI_ENABLED") == "1",
            os.getenv("RP1_AI_ENDPOINT", cls.endpoint),
            os.getenv("RP1_AI_MODEL", ""),
            os.getenv("RP1_AI_API_KEY", ""),
            api=os.getenv("RP1_AI_API", "openai-responses"),
        )

    def validate(self):
        parsed = urlparse(self.endpoint)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ModelError("模型地址无效；不能携带凭据、查询参数或片段")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ModelError("模型服务须使用 HTTPS；本地模型允许回环 HTTP")
        if self.api not in {"openai-responses", "openai-completions"}:
            raise ModelError("不支持该模型协议")
        if not self.model.strip():
            raise ModelError("请填写模型 ID，或在服务端配置 RP1_AI_MODEL")
        if not self.api_key.isascii() or any(
            ord(char) < 33 or ord(char) == 127 for char in self.api_key
        ):
            raise ModelError("API Key 格式无效")


# IDs and endpoints are a catalog, not a claim that a given account can call them.
PRESETS = {
    "deepseek": dict(
        name="DeepSeek",
        baseUrl="https://api.deepseek.com",
        api="openai-completions",
        apiKeyEnv="DEEPSEEK_API_KEY",
        models=[
            dict(id="deepseek-flash", name="DeepSeek Flash"),
            dict(id="deepseek-v4-pro", name="DeepSeek V4 Pro"),
        ],
        compat={"thinking": "deepseek"},
    ),
    "qwen": dict(
        name="千问 · 北京",
        baseUrl="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api="openai-completions",
        apiKeyEnv="DASHSCOPE_API_KEY",
        models=[dict(id="qwen-plus", name="Qwen Plus"), dict(id="qwen-flash", name="Qwen Flash")],
        compat={"thinking": "qwen"},
    ),
    "qwen-intl": dict(
        name="千问 · 新加坡",
        baseUrl="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api="openai-completions",
        apiKeyEnv="DASHSCOPE_INTL_API_KEY",
        models=[dict(id="qwen-plus", name="Qwen Plus")],
        compat={"thinking": "qwen"},
    ),
    "openai": dict(
        name="OpenAI",
        baseUrl="https://api.openai.com/v1",
        api="openai-responses",
        apiKeyEnv="OPENAI_API_KEY",
        models=[],
    ),
}


class ModelRegistry:
    def __init__(self, legacy=None, config_path=None):
        self.legacy = legacy or AISettings.from_env()
        replace(self.legacy, model="validate").validate()
        self.providers = {key: dict(value) for key, value in PRESETS.items()}
        path = config_path or os.getenv("RP1_AI_MODELS_FILE")
        if path:
            # Only operator-owned configuration can introduce a network destination.
            data = json.loads(Path(path).read_text())
            providers = data.get("providers", {})
            if not isinstance(providers, dict) or len(providers) > 32:
                raise ModelError("模型配置必须包含 providers 对象，最多 32 个服务")
            for key, raw in providers.items():
                ModelConnection(provider=key)
                if key == "server" or not isinstance(raw, dict) or "apiKey" in raw:
                    raise ModelError(
                        "自定义服务请使用 apiKeyEnv；不能覆盖 server 或保存明文 apiKey"
                    )
                if set(raw) - {
                    "name",
                    "baseUrl",
                    "api",
                    "apiKeyEnv",
                    "models",
                    "compat",
                    "requiresKey",
                }:
                    raise ModelError("模型配置含不支持的字段")
                item = {**raw, "name": raw.get("name", key), "models": raw.get("models", [])}
                base = str(item["baseUrl"]).rstrip("/")
                api = item.get("api", "openai-completions")
                endpoint = base + (
                    "/responses" if api == "openai-responses" else "/chat/completions"
                )
                AISettings(True, endpoint, "validate", api=api).validate()
                compat = item.get("compat", {})
                if not isinstance(compat, dict) or set(compat) - {
                    "thinking",
                    "supportsStrictMode",
                    "supportsToolChoice",
                    "maxTokensField",
                }:
                    raise ModelError("模型 compat 配置无效")
                if compat.get("maxTokensField", "max_tokens") not in {
                    "max_tokens",
                    "max_completion_tokens",
                }:
                    raise ModelError("maxTokensField 配置无效")
                if compat.get("thinking") not in {None, "deepseek", "qwen"}:
                    raise ModelError("thinking 配置无效")
                if not isinstance(item["models"], list) or len(item["models"]) > 64:
                    raise ModelError("每个服务最多登记 64 个模型")
                for model in item["models"]:
                    ModelConnection(model=model["id"])
                item.update(baseUrl=base, api=api)
                self.providers[key] = item
        default = os.getenv("RP1_AI_PROVIDER", "server")
        if self.legacy.enabled and default != "server":
            self.legacy = self.resolve(
                ModelConnection(
                    provider=default, model=self.legacy.model, api_key=self.legacy.api_key
                )
            )

    def catalog(self):
        items = [
            dict(
                id="server",
                name="服务端默认",
                api=self.legacy.api,
                endpoint=self.legacy.endpoint,
                models=[dict(id=self.legacy.model, name=self.legacy.model)]
                if self.legacy.model
                else [],
                configured=self.legacy.enabled,
                requires_key=not bool(self.legacy.api_key),
            )
        ]
        for key, item in self.providers.items():
            items.append(
                dict(
                    id=key,
                    name=item["name"],
                    api=item["api"],
                    endpoint=self.endpoint(item),
                    models=item["models"],
                    configured=bool(os.getenv(item.get("apiKeyEnv", ""))),
                    requires_key=item.get("requiresKey", True),
                )
            )
        return {"items": items}

    @staticmethod
    def endpoint(item):
        return item["baseUrl"].rstrip("/") + (
            "/responses" if item["api"] == "openai-responses" else "/chat/completions"
        )

    def resolve(self, connection=None):
        if connection is None:
            return self.legacy
        if connection.provider == "server":
            key = connection.api_key.get_secret_value() or self.legacy.api_key
            result = replace(
                self.legacy,
                model=connection.model.strip() or self.legacy.model,
                api_key=key,
                enabled=self.legacy.enabled or bool(key),
            )
        else:
            item = self.providers.get(connection.provider)
            if item is None:
                raise ModelError("模型服务未登记，请刷新服务端配置")
            # Never fall back to another provider's credential.
            key = connection.api_key.get_secret_value() or os.getenv(item.get("apiKeyEnv", ""), "")
            if item.get("requiresKey", True) and not key:
                raise ModelError("该服务需要 API Key；请在本窗口填写，或配置对应服务端环境变量")
            model = connection.model.strip() or (item["models"][0]["id"] if item["models"] else "")
            result = AISettings(
                True,
                self.endpoint(item),
                model,
                key,
                connection.provider,
                item["api"],
                item.get("compat", {}),
            )
        result.validate()
        return result


class ModelSession:
    """Normalize two API formats while preserving native tool-call messages."""

    def __init__(self, settings, messages, max_tokens=1600):
        self.settings, self.messages = settings, list(messages)
        self.max_tokens = max_tokens
        self.last_message = None

    def exchange(self, client, instructions, tools, turn):
        settings = self.settings
        choice = "required" if turn == 0 else "none" if turn == 2 else "auto"
        if settings.api == "openai-responses":
            payload = dict(
                model=settings.model,
                instructions=instructions,
                input=self.messages,
                tools=tools,
                tool_choice=choice,
                parallel_tool_calls=False,
                store=False,
                max_output_tokens=self.max_tokens,
            )
        else:
            functions = []
            for tool in tools:
                function = {key: tool[key] for key in ("name", "description", "parameters")}
                if settings.compat.get("supportsStrictMode"):
                    function["strict"] = True
                functions.append({"type": "function", "function": function})
            payload = dict(
                model=settings.model,
                messages=[{"role": "system", "content": instructions}] + self.messages,
                tools=functions,
                stream=False,
            )
            payload[settings.compat.get("maxTokensField", "max_tokens")] = self.max_tokens
            if settings.compat.get("supportsToolChoice", True):
                payload["tool_choice"] = choice
            if settings.compat.get("thinking") == "deepseek":
                payload["thinking"] = {"type": "disabled"}
            elif settings.compat.get("thinking") == "qwen":
                payload["enable_thinking"] = False
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        try:
            with client.stream(
                "POST", settings.endpoint, headers=headers, json=payload
            ) as response:
                if response.status_code in {401, 403}:
                    raise ModelError("模型鉴权失败：请核对所选厂商、地域、API Key 与模型权限")
                if response.status_code == 429:
                    raise ModelError("模型服务限流或额度不足，请核对账户额度后重试")
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ModelError("模型响应超过限制")
            result = json.loads(body)
            if settings.api == "openai-responses":
                if result.get("status") in {"incomplete", "failed", "cancelled"}:
                    raise ModelError("模型未完成回答")
                output = result.get("output", [])
            else:
                choices = result.get("choices", [])
                if len(choices) != 1 or choices[0].get("finish_reason") not in {
                    "stop",
                    "tool_calls",
                }:
                    raise ModelError("模型回答被截断或结构不兼容，请核对模型的工具调用支持")
                raw = choices[0]["message"]
                if raw.get("role") != "assistant":
                    raise ModelError("模型消息角色无效")
                self.last_message = {
                    key: raw[key]
                    for key in ("role", "content", "tool_calls", "reasoning_content")
                    if key in raw
                }
                output = [
                    {
                        "type": "function_call",
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                        "call_id": call["id"],
                    }
                    for call in raw.get("tool_calls", [])
                ]
                if raw.get("content"):
                    output.append(
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": raw["content"]}],
                        }
                    )
            if (
                not isinstance(output, list)
                or len(output) > 32
                or not all(isinstance(item, dict) for item in output)
            ):
                raise ModelError("模型输出结构无效")
            return output
        except httpx.TimeoutException as exc:
            raise ModelError("模型请求超时，问题已保留，可稍后重试") from exc
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise ModelError("模型响应格式不兼容，请核对 API 协议与模型的工具调用支持") from exc

    def add_tool_result(self, output, call_id, content):
        if self.settings.api == "openai-responses":
            self.messages.extend(output)
            self.messages.append(
                {"type": "function_call_output", "call_id": call_id, "output": content}
            )
        else:
            self.messages.append(self.last_message)
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
