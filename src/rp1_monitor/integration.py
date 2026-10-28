"""Optional same-origin mount; production platform is never modified automatically."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .backend import BackendSettings, create_app


def mount_monitoring(
    platform_app: FastAPI,
    settings: BackendSettings,
    *,
    read_dependency,
    path: str = "/monitor",
) -> FastAPI:
    """Reuse a human read dependency, keep device writes independently scoped.

    Compose the host lifespan: mounted ASGI lifespans do not run automatically.
    """
    if settings.demo_read or not callable(read_dependency):
        raise ValueError("平台挂载必须提供实际只读认证依赖，禁止匿名演示")
    if not path.startswith("/") or path == "/" or path.endswith("/"):
        raise ValueError("挂载路径应类似 /monitor")
    if settings.initialize:
        raise ValueError("平台挂载禁止自动建表；先运行独立迁移，再设置 initialize=False")
    child = create_app(settings, read_dependency=read_dependency)
    # Platform-specific auth exceptions retain the host's HTTP error response.
    for exception, handler in platform_app.exception_handlers.items():
        if exception not in child.exception_handlers:
            child.add_exception_handler(exception, handler)
    platform_app.mount(path, child, name="rp1-monitor")
    host_lifespan = platform_app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app):
        async with host_lifespan(app) as state:
            child.state.store.alert_monitor.start()
            try:
                yield state
            finally:
                child.state.store.alert_monitor.stop()
                if hasattr(child.state, "administration"):
                    child.state.administration.close()
                child.state.store.engine.dispose()

    platform_app.router.lifespan_context = combined_lifespan
    return child
