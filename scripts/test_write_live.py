#!/usr/bin/env python3
"""Manual integration test: send a message to a live Claude Code session.

Usage:
    python scripts/test_write_live.py [--session-id <id>] [--message "hello"]

What it does:
    1. Lists all active Claude Code sessions
    2. Picks the most recently modified session (or the one you specify)
    3. Sends a test message via send_input_async() (claude --print --resume)
    4. Tails the session file to observe the response arriving via JSONL
    5. Reports the result

This is a semi-automated test — it verifies the write path works end-to-end
by invoking the Claude CLI with --resume and monitoring the session file.

Press Ctrl+C to stop.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from devcon_server import claude_bridge


def print_message(msg: dict, prefix: str = "") -> None:
    """Pretty-print a structured message."""
    role = msg.get("role", "?")
    uuid = msg.get("uuid", "?")[:12]
    ts = msg.get("timestamp", "?")
    model = msg.get("model")

    role_color = "\033[94m" if role == "user" else "\033[92m"
    reset = "\033[0m"

    header = f"{prefix}{role_color}[{role.upper()}]{reset} {ts}"
    if model:
        header += f"  (model={model})"
    print(header, flush=True)

    for block in msg.get("content", []):
        btype = block.get("type", "?")
        if btype == "text":
            text = block.get("text", "")
            if len(text) > 500:
                text = text[:500] + f"... ({len(text)} chars total)"
            print(f"  {text}")
        elif btype == "thinking":
            thinking = block.get("thinking", "")
            if len(thinking) > 200:
                thinking = thinking[:200] + "..."
            print(f"  [thinking] {thinking}")
        elif btype == "tool_use":
            print(f"  [tool_use] {block.get('name', '?')}({json.dumps(block.get('input', {}))[:100]})")
        elif btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + "..."
            print(f"  [tool_result] {content}")
    print()


async def on_new_messages(messages: list[dict]) -> None:
    """Callback for tail_session — prints new messages as they arrive."""
    print(f"\n{'='*60}")
    print(f"  TAIL DETECTED NEW MESSAGES ({len(messages)})")
    print(f"{'='*60}\n")
    for msg in messages:
        print_message(msg, prefix="  >> ")


async def run(session_id: str | None = None, message: str = "Hello from mobile test!") -> None:
    # Step 1: List sessions
    logger.info("Scanning for Claude Code sessions...")
    sessions = claude_bridge.list_sessions()

    if not sessions:
        print("No Claude Code sessions found in ~/.claude/projects/")
        print("Start a Claude Code session in VSCode first, then re-run this script.")
        return

    # Step 2: Pick session
    if session_id:
        matching = [s for s in sessions if s["id"] == session_id]
        if not matching:
            print(f"Session not found: {session_id}")
            print("Available sessions:")
            for s in sessions[:10]:
                print(f"  {s['id']}  —  {s.get('title', '(no title)')}")
            return
        chosen = matching[0]
    else:
        chosen = sessions[0]
        print(f"Found {len(sessions)} sessions. Using most recent:\n")

    print(f"  Session ID: {chosen['id']}")
    print(f"  Title:      {chosen.get('title', '(no title)')}")
    print(f"  Project:    {chosen.get('project_path', '?')}")
    print(f"  File:       {chosen.get('file', '?')}")
    print()

    # Step 3: Start tailing in background
    print(f"{'='*60}")
    print(f"  Starting tail watcher...")
    print(f"{'='*60}\n")

    tail_task = asyncio.create_task(
        claude_bridge.tail_session(chosen["id"], on_new_messages)
    )

    # Step 4: Send the test message
    print(f"Sending message: {message!r}")
    print(f"{'='*60}\n")

    start = time.monotonic()
    result = await claude_bridge.send_input_async(chosen["id"], message)
    duration = time.monotonic() - start

    print(f"\n{'='*60}")
    print(f"  WRITE RESULT")
    print(f"{'='*60}")
    print(f"  Success:    {result.get('success')}")
    print(f"  Return code: {result.get('returncode')}")
    print(f"  Duration:   {duration:.1f}s")
    if result.get("error"):
        print(f"  Error:      {result['error']}")
    if result.get("stderr"):
        stderr = result["stderr"]
        if len(stderr) > 500:
            stderr = stderr[:500] + "..."
        print(f"  Stderr:     {stderr}")
    print()

    # Step 5: Wait a bit for tail to catch up
    print("Waiting 3s for tail to detect response in JSONL...")
    await asyncio.sleep(3)

    # Cancel tail
    tail_task.cancel()
    try:
        await tail_task
    except asyncio.CancelledError:
        pass

    print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the write path to a live Claude Code session")
    parser.add_argument("--session-id", help="Specific session ID to write to (default: most recent)")
    parser.add_argument("--message", default="Hello from mobile test!", help="Message to send")
    parser.add_argument("--log-level", default="INFO", choices=["TRACE", "DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format=(
        "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>"
    ))
    logger.add(
        "/tmp/vsclaudemobile/test_write_live.log",
        level="TRACE",
        rotation="5 MB",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    try:
        asyncio.run(run(session_id=args.session_id, message=args.message))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
