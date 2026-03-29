"""Interceptor client — connects to hub_server and bridges Claude Code sessions."""

import asyncio
import json
import logging
import os
import socket
from typing import Any

import websockets
from websockets.asyncio.client import connect

from . import claude_bridge

logger = logging.getLogger(__name__)


class InterceptorClient:
    """WebSocket client that connects to the hub and serves session data."""

    def __init__(self, hub_host: str, hub_port: int):
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.window_id = os.environ.get("WINDOW_ID", socket.gethostname())
        self._ws: websockets.ClientConnection | None = None
        self._tail_tasks: dict[str, asyncio.Task] = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.hub_host}:{self.hub_port}/ws/interceptor/{self.window_id}"

    async def _send(self, msg: dict[str, Any]) -> None:
        """Send a JSON message over the WebSocket."""
        if self._ws is None:
            logger.error("Cannot send — not connected")
            return
        payload = json.dumps(msg)
        await self._ws.send(payload)
        logger.debug("Sent: %s", payload[:200])

    # ── Message handlers ──────────────────────────────────────────────

    async def _handle_session_list(self, msg: dict[str, Any]) -> None:
        """Respond to a session_list request with discovered sessions."""
        sessions = claude_bridge.list_sessions()
        await self._send(
            {
                "type": "session_list_response",
                "request_id": msg.get("request_id"),
                "sessions": sessions,
            }
        )

    async def _handle_session_select(self, msg: dict[str, Any]) -> None:
        """Start tailing a session and send its current history."""
        session_id = msg.get("session_id")
        if not session_id:
            logger.warning("session_select missing session_id")
            return

        # Send current history
        history = claude_bridge.read_session(session_id)
        await self._send(
            {
                "type": "session_output",
                "session_id": session_id,
                "messages": history,
                "full_history": True,
            }
        )

        # Cancel any existing tail for a different session
        self._cancel_tail(session_id)

        # Start tailing for new messages
        task = asyncio.create_task(
            claude_bridge.tail_session(session_id, self._make_tail_callback(session_id))
        )
        self._tail_tasks[session_id] = task
        logger.info("Now tailing session %s", session_id)

    async def _handle_session_input(self, msg: dict[str, Any]) -> None:
        """Send user input to a Claude Code session."""
        session_id = msg.get("session_id")
        text = msg.get("text", "")
        if not session_id or not text:
            logger.warning("session_input missing session_id or text")
            return

        logger.info("Received input for session %s: %s", session_id, text[:80])
        # Run the blocking CLI call in a thread
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, claude_bridge.send_input, session_id, text
        )
        await self._send(
            {
                "type": "session_input_result",
                "session_id": session_id,
                "result": result,
            }
        )

    # ── Tail callback ─────────────────────────────────────────────────

    def _make_tail_callback(self, session_id: str):
        async def callback(new_messages: list[dict[str, Any]]) -> None:
            await self._send(
                {
                    "type": "session_output",
                    "session_id": session_id,
                    "messages": new_messages,
                    "full_history": False,
                }
            )

        return callback

    def _cancel_tail(self, keep_session_id: str | None = None) -> None:
        """Cancel running tail tasks, optionally keeping one."""
        for sid, task in list(self._tail_tasks.items()):
            if sid != keep_session_id and not task.done():
                task.cancel()
                logger.info("Cancelled tail for session %s", sid)
        self._tail_tasks = {
            sid: t
            for sid, t in self._tail_tasks.items()
            if sid == keep_session_id and not t.done()
        }

    # ── Main loop ─────────────────────────────────────────────────────

    _HANDLERS = {
        "session_list": "_handle_session_list",
        "session_select": "_handle_session_select",
        "session_input": "_handle_session_input",
    }

    async def _dispatch(self, raw: str) -> None:
        """Parse and dispatch an incoming message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON message: %s", raw[:100])
            return

        msg_type = msg.get("type")
        handler_name = self._HANDLERS.get(msg_type)
        if handler_name:
            handler = getattr(self, handler_name)
            await handler(msg)
        else:
            logger.warning("Unknown message type: %s", msg_type)

    async def run(self) -> None:
        """Connect to the hub and process messages. Reconnects on failure."""
        while True:
            try:
                logger.info("Connecting to hub at %s", self.ws_url)
                async with connect(self.ws_url) as ws:
                    self._ws = ws
                    logger.info(
                        "Connected to hub (window_id=%s)", self.window_id
                    )
                    async for message in ws:
                        await self._dispatch(message)
            except websockets.ConnectionClosed as e:
                logger.warning("Hub connection closed: %s", e)
            except OSError as e:
                logger.error("Connection error: %s", e)
            finally:
                self._ws = None
                self._cancel_tail()

            logger.info("Reconnecting in 3 seconds...")
            await asyncio.sleep(3)
