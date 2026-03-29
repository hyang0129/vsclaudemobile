"""Devcon server client — connects to hub_server and bridges Claude Code sessions."""

import asyncio
import json
import os
import socket
import time
from typing import Any

import websockets
from loguru import logger
from websockets.asyncio.client import connect

from . import claude_bridge


class DevconClient:
    """WebSocket client that connects to the hub and serves session data."""

    def __init__(self, hub_host: str, hub_port: int):
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.window_id = os.environ.get("WINDOW_ID", socket.gethostname())
        self._ws: websockets.ClientConnection | None = None
        self._tail_tasks: dict[str, asyncio.Task] = {}
        self._active_writes: dict[str, asyncio.Task] = {}
        logger.info("DevconClient initialized: hub={}:{}, window_id={}", hub_host, hub_port, self.window_id)

    @property
    def ws_url(self) -> str:
        return f"ws://{self.hub_host}:{self.hub_port}/ws/devcon/{self.window_id}"

    async def _send(self, msg: dict[str, Any]) -> None:
        """Send a JSON message over the WebSocket."""
        if self._ws is None:
            logger.error("Cannot send — not connected")
            return
        payload = json.dumps(msg)
        await self._ws.send(payload)
        logger.debug("Sent message type={}, len={}", msg.get("type"), len(payload))
        logger.trace("Sent payload: {}", payload[:500])

    # ── Message handlers ──────────────────────────────────────────────

    async def _handle_session_list(self, msg: dict[str, Any]) -> None:
        """Respond to a session_list request with discovered sessions."""
        logger.info("Handling session_list request (request_id={})", msg.get("request_id"))
        sessions = claude_bridge.list_sessions()
        logger.info("Returning {} sessions for session_list", len(sessions))
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

        logger.info("Handling session_select: session_id={}", session_id)

        # Send current history
        history = claude_bridge.read_session(session_id)
        logger.info("Sending full history: {} messages for session {}", len(history), session_id)
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
        logger.info("Now tailing session {} (active tails: {})", session_id, len(self._tail_tasks))

    async def _handle_session_input(self, msg: dict[str, Any]) -> None:
        """Send user input to a Claude Code session (async write path)."""
        session_id = msg.get("session_id")
        text = msg.get("text", "")
        if not session_id or not text:
            logger.warning("session_input missing session_id or text")
            return

        logger.info("Received input for session {}: {!r}", session_id, text[:80])

        # Concurrent write guard (per-session)
        if session_id in self._active_writes:
            logger.warning("Rejecting concurrent write to session {}", session_id)
            await self._send({
                "type": "write_ended",
                "session_id": session_id,
                "success": False,
                "error": "A write is already in progress for this session. Please wait for it to complete.",
                "duration_ms": None,
                "returncode": None,
            })
            return

        # Signal write started
        await self._send({"type": "write_started", "session_id": session_id})

        # Run the write as an async task
        task = asyncio.create_task(self._execute_write(session_id, text))
        self._active_writes[session_id] = task

    async def _execute_write(self, session_id: str, text: str) -> None:
        """Execute a write operation and send write_ended when complete."""
        start_time = time.monotonic()
        try:
            result = await claude_bridge.send_input_async(session_id, text)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.info("Write completed for session {}: success={}, duration={}ms",
                        session_id, result.get("success"), duration_ms)
            await self._send({
                "type": "write_ended",
                "session_id": session_id,
                "success": result.get("success", False),
                "error": result.get("error") or (result.get("stderr") if not result.get("success") else None),
                "duration_ms": duration_ms,
                "returncode": result.get("returncode"),
            })
        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error("Write failed for session {}: {}", session_id, e)
            await self._send({
                "type": "write_ended",
                "session_id": session_id,
                "success": False,
                "error": str(e),
                "duration_ms": duration_ms,
                "returncode": None,
            })
        finally:
            self._active_writes.pop(session_id, None)

    # ── Tail callback ─────────────────────────────────────────────────

    def _make_tail_callback(self, session_id: str):
        async def callback(new_messages: list[dict[str, Any]]) -> None:
            logger.info("Tail callback: sending {} new messages for session {}", len(new_messages), session_id)
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
                logger.info("Cancelled tail for session {}", sid)
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
            logger.warning("Received non-JSON message: {}", raw[:100])
            return

        msg_type = msg.get("type")
        logger.debug("Dispatching message type={}", msg_type)
        handler_name = self._HANDLERS.get(msg_type)
        if handler_name:
            handler = getattr(self, handler_name)
            await handler(msg)
        else:
            logger.warning("Unknown message type: {}", msg_type)

    async def run(self) -> None:
        """Connect to the hub and process messages. Reconnects on failure."""
        logger.info("DevconClient.run() starting, target={}", self.ws_url)
        while True:
            try:
                logger.info("Connecting to hub at {}", self.ws_url)
                async with connect(self.ws_url) as ws:
                    self._ws = ws
                    logger.info("Connected to hub (window_id={})", self.window_id)
                    async for message in ws:
                        logger.trace("Raw WS message received: {}", message[:200] if isinstance(message, str) else "<binary>")
                        await self._dispatch(message)
            except websockets.ConnectionClosed as e:
                logger.warning("Hub connection closed: {}", e)
            except OSError as e:
                logger.error("Connection error: {}", e)
            finally:
                self._ws = None
                self._cancel_tail()

            logger.info("Reconnecting in 3 seconds...")
            await asyncio.sleep(3)
