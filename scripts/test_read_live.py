#!/usr/bin/env python3
"""Manual integration test: watch for a new message in a live Claude Code session.

Usage:
    python scripts/test_read_live.py [--session-id <id>]

What it does:
    1. Lists all active Claude Code sessions
    2. Picks the most recently modified session (or the one you specify)
    3. Reads and displays the current conversation history
    4. Tails the session file, waiting for new messages
    5. When the human sends a message through the VSCode Claude extension,
       the script detects and displays it

This is a semi-automated test — it requires a human to send a message through
the VSCode Claude Code extension to verify the read path works end-to-end.

Press Ctrl+C to stop.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from interceptor import claude_bridge


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
            # Truncate long text
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
    print(f"  NEW MESSAGES DETECTED ({len(messages)})")
    print(f"{'='*60}\n")
    for msg in messages:
        print_message(msg, prefix="  >> ")


async def run(session_id: str | None = None) -> None:
    # Step 1: List sessions
    logger.info("Scanning for Claude Code sessions...")
    sessions = claude_bridge.list_sessions()

    if not sessions:
        print("No Claude Code sessions found in ~/.claude/projects/")
        print("Start a Claude Code session in VSCode first, then re-run this script.")
        return

    # Step 2: Pick session
    if session_id:
        # Verify it exists
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

    # Step 3: Read current history
    logger.info("Reading current conversation history...")
    messages = claude_bridge.read_session(chosen["id"])
    print(f"Current history: {len(messages)} messages")
    print(f"{'-'*60}")

    # Show last 5 messages as context
    recent = messages[-5:] if len(messages) > 5 else messages
    if len(messages) > 5:
        print(f"  (showing last 5 of {len(messages)} messages)\n")
    for msg in recent:
        print_message(msg, prefix="  ")

    # Step 4: Tail for new messages
    print(f"{'='*60}")
    print("  WAITING FOR NEW MESSAGES...")
    print("  Send a message in the VSCode Claude Code extension")
    print("  to verify the read path works.")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'='*60}\n")

    try:
        await claude_bridge.tail_session(chosen["id"], on_new_messages)
    except asyncio.CancelledError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a live Claude Code session for new messages")
    parser.add_argument("--session-id", help="Specific session ID to watch (default: most recent)")
    parser.add_argument("--log-level", default="INFO", choices=["TRACE", "DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    # Ensure print() output is visible immediately (not buffered)
    sys.stdout.reconfigure(line_buffering=True)

    # Configure loguru
    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format=(
        "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>"
    ))
    logger.add(
        "/tmp/vsclaudemobile/test_read_live.log",
        level="TRACE",
        rotation="5 MB",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    try:
        asyncio.run(run(session_id=args.session_id))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
