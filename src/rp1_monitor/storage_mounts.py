"""Explicit local mounts; never infer a remote database's free space from the API host."""

import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from .models import Model


class Mount(Model):
    path: Path
    device_id: int = Field(ge=0)
    reserve_bytes: int = Field(ge=0)


class StorageMounts(Model):
    database_authority: str
    mounts: dict[str, Mount] = Field(min_length=1, max_length=2)

    @classmethod
    def load(cls, path, url):
        config = cls.model_validate_json(Path(path).read_text())
        if url.get_backend_name() != "postgresql" or url.host not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("本机挂载盘探测只允许显式绑定的本机PostgreSQL部署")
        if config.database_authority != f"{url.host}:{url.port or 5432}/{url.database}":
            raise ValueError("磁盘探测配置与数据库连接不匹配")
        if set(config.mounts) != {"database", "archives"} or any(
            not item.path.is_absolute() for item in config.mounts.values()
        ):
            raise ValueError("必须明确绑定数据库卷和归档目录的绝对路径")
        return config

    def observe(self):
        result = []
        for name, item in self.mounts.items():
            entry = {
                "name": name,
                "source": "explicit_local_deployment_mount",
                "state": "unknown",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "free_bytes": None,
                "total_bytes": None,
                "reserve_bytes": item.reserve_bytes,
            }
            try:
                if item.path.stat().st_dev != item.device_id:
                    raise ValueError("Mount identity changed")
                values = os.statvfs(item.path)
                entry.update(
                    state="measured",
                    free_bytes=values.f_bavail * values.f_frsize,
                    total_bytes=values.f_blocks * values.f_frsize,
                )
                entry["below_reserve"] = entry["free_bytes"] < item.reserve_bytes
            except (OSError, ValueError):
                entry["below_reserve"] = None
            result.append(entry)
        measured = all(item["state"] == "measured" for item in result)
        labels = {"database": "数据库盘", "archives": "归档目录盘"}
        detail = " · ".join(
            f"{labels[item['name']]}剩余 {item['free_bytes'] / 1024**3:.1f} GiB"
            + ("（低于预留量）" if item["below_reserve"] else "")
            if item["free_bytes"] is not None
            else f"{labels[item['name']]}剩余未知"
            for item in result
        )
        database = next(item for item in result if item["name"] == "database")
        return {
            "state": "measured" if measured else "unknown",
            "free_bytes": database["free_bytes"],
            "mounts": result,
            "detail": detail + "。来自显式绑定的实际数据卷；同一磁盘的容量不能相加。",
        }
