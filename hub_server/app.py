"""Hub server: relays messages between interceptors (devcontainers) and mobile clients."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("hub_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Hub Server", version="0.1.0")


class ConnectionManager:
    """Tracks connected interceptors and mobile clients by window_id."""

    def __init__(self):
        # window_id -> WebSocket
        self.interceptors: dict[str, WebSocket] = {}
        # window_id -> list[WebSocket]
        self.mobile_clients: dict[str, list[WebSocket]] = {}
        # For proxied REST requests waiting on interceptor responses.
        # key: (window_id, request_id) -> asyncio.Future
        self.pending_requests: dict[tuple[str, str], asyncio.Future] = {}
        self._request_counter = 0

    # -- interceptor management --

    def connect_interceptor(self, window_id: str, ws: WebSocket):
        self.interceptors[window_id] = ws
        logger.info("Interceptor connected: %s", window_id)

    def disconnect_interceptor(self, window_id: str):
        self.interceptors.pop(window_id, None)
        logger.info("Interceptor disconnected: %s", window_id)

    def get_interceptor(self, window_id: str) -> Optional[WebSocket]:
        return self.interceptors.get(window_id)

    # -- mobile management --

    def connect_mobile(self, window_id: str, ws: WebSocket):
        self.mobile_clients.setdefault(window_id, []).append(ws)
        logger.info(
            "Mobile client connected to window %s (total: %d)",
            window_id,
            len(self.mobile_clients[window_id]),
        )

    def disconnect_mobile(self, window_id: str, ws: WebSocket):
        clients = self.mobile_clients.get(window_id, [])
        if ws in clients:
            clients.remove(ws)
        if not clients:
            self.mobile_clients.pop(window_id, None)
        logger.info("Mobile client disconnected from window %s", window_id)

    def get_mobile_clients(self, window_id: str) -> list[WebSocket]:
        return self.mobile_clients.get(window_id, [])

    # -- proxied request helpers --

    def next_request_id(self) -> str:
        self._request_counter += 1
        return f"req_{self._request_counter}"

    def create_pending_request(self, window_id: str, request_id: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_requests[(window_id, request_id)] = future
        return future

    def resolve_pending_request(self, window_id: str, request_id: str, data):
        key = (window_id, request_id)
        future = self.pending_requests.pop(key, None)
        if future and not future.done():
            future.set_result(data)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------


@app.websocket("/ws/interceptor/{window_id}")
async def ws_interceptor(websocket: WebSocket, window_id: str):
    await websocket.accept()
    manager.connect_interceptor(window_id, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON message from interceptor %s", window_id)
                continue

            msg_type = message.get("type")
            logger.debug("Interceptor %s -> type=%s", window_id, msg_type)

            # If this is a response to a proxied REST request, resolve it.
            request_id = message.get("request_id")
            if request_id:
                manager.resolve_pending_request(window_id, request_id, message)
                continue

            # Otherwise relay to all connected mobile clients for this window.
            clients = manager.get_mobile_clients(window_id)
            dead: list[WebSocket] = []
            for client in clients:
                try:
                    await client.send_text(raw)
                except Exception:
                    dead.append(client)
            for d in dead:
                manager.disconnect_mobile(window_id, d)

    except WebSocketDisconnect:
        manager.disconnect_interceptor(window_id)
    except Exception:
        logger.exception("Interceptor ws error for %s", window_id)
        manager.disconnect_interceptor(window_id)


@app.websocket("/ws/mobile/{window_id}")
async def ws_mobile(websocket: WebSocket, window_id: str):
    await websocket.accept()
    manager.connect_mobile(window_id, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON message from mobile for window %s", window_id)
                continue

            msg_type = message.get("type")
            logger.debug("Mobile -> interceptor %s, type=%s", window_id, msg_type)

            # Relay to the interceptor for this window.
            interceptor = manager.get_interceptor(window_id)
            if interceptor is None:
                err = json.dumps({"type": "error", "message": f"No interceptor connected for window {window_id}"})
                await websocket.send_text(err)
                continue

            try:
                await interceptor.send_text(raw)
            except Exception:
                logger.exception("Failed to relay to interceptor %s", window_id)
                manager.disconnect_interceptor(window_id)
                err = json.dumps({"type": "error", "message": f"Interceptor for window {window_id} disconnected"})
                await websocket.send_text(err)

    except WebSocketDisconnect:
        manager.disconnect_mobile(window_id, websocket)
    except Exception:
        logger.exception("Mobile ws error for window %s", window_id)
        manager.disconnect_mobile(window_id, websocket)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/api/windows")
async def list_windows():
    """Return list of connected interceptor window_ids."""
    return {"windows": list(manager.interceptors.keys())}


@app.get("/api/windows/{window_id}/sessions")
async def list_sessions(window_id: str):
    """Proxy a session_list request to the interceptor and return the response."""
    interceptor = manager.get_interceptor(window_id)
    if interceptor is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"No interceptor connected for window {window_id}"},
        )

    request_id = manager.next_request_id()
    future = manager.create_pending_request(window_id, request_id)

    request_msg = json.dumps({
        "type": "session_list",
        "request_id": request_id,
    })

    try:
        await interceptor.send_text(request_msg)
    except Exception:
        logger.exception("Failed to send session_list to interceptor %s", window_id)
        manager.disconnect_interceptor(window_id)
        return JSONResponse(
            status_code=502,
            content={"error": "Failed to reach interceptor"},
        )

    try:
        result = await asyncio.wait_for(future, timeout=10.0)
    except asyncio.TimeoutError:
        manager.pending_requests.pop((window_id, request_id), None)
        return JSONResponse(
            status_code=504,
            content={"error": "Interceptor did not respond in time"},
        )

    return result
