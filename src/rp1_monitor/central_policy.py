"""Server-owned monitoring policy. Defaults are design parameters, not robot limits."""

import fnmatch
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_PATTERNS = [
    "battery.*",
    "host.*",
    "process.*",
    "joint.*.motor_temperature",
    "joint.*.mos_temperature",
    "joint.*.fault_code",
]


class CentralPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = Field(default=1, ge=1, le=1)
    key_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PATTERNS), min_length=1, max_length=64
    )
    retention_days: int = Field(default=30, ge=1, le=3650)
    alert_hold_seconds: int = Field(default=4, ge=0, le=3600)
    pending_windows_warning: int = Field(default=300, ge=1)
    status_max_age_seconds: int = Field(default=15, ge=5, le=300)
    # Real thermal/electrical limits must come from the actual robot specification.
    temperature_limits_c: dict[str, float] = Field(default_factory=dict, max_length=128)

    @field_validator("key_patterns")
    @classmethod
    def safe_patterns(cls, values):
        import re

        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"(?:joint|imu|battery|host|process)\.[A-Za-z0-9_.*]{1,100}", v)
            for v in values
        ):
            raise ValueError("关键指标白名单只接受限定遥测前缀、字母、数字、下划线、点和通配符*")
        return values

    @field_validator("temperature_limits_c")
    @classmethod
    def valid_limits(cls, values):
        import math

        from pydantic import TypeAdapter

        from .models import MetricKey

        for key, value in values.items():
            TypeAdapter(MetricKey).validate_python(key)
            if not key.endswith(("temperature",)) or not math.isfinite(value):
                raise ValueError("温度限值必须绑定已知温度指标且为有限摄氏度值")
        return values

    def selects(self, key):
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in self.key_patterns)

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def load_central_policy(path: Path | None):
    return CentralPolicy.model_validate_json(path.read_text()) if path else CentralPolicy()
