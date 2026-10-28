"""Shared loopback preview API for the robot observer and computer relay."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect


def create_preview_app(runtime, title):
    @asynccontextmanager
    async def lifespan(_):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/api/v1/state")
    def state() -> dict:
        return runtime.snapshot().model_dump(mode="json")

    @app.websocket("/api/v1/telemetry")
    async def telemetry(websocket: WebSocket):
        if websocket.headers.get("origin"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                try:
                    frame = runtime.snapshot()
                except HTTPException:
                    await websocket.close(code=1013)
                    return
                await websocket.send_json(frame.model_dump(mode="json"))
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    from .local_operations import register_local_operations

    register_local_operations(app, runtime)
    return app
