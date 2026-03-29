"""Interface to Claude Code CLI and session data."""

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
POLL_INTERVAL = 0.5  # seconds


def _find_session_files() -> list[Path]:
    """Find all JSONL conversation files under ~/.claude/projects/."""
    if not CLAUDE_PROJECTS_DIR.exists():
        logger.warning("Claude projects dir not found: %s", CLAUDE_PROJECTS_DIR)
        return []
    return sorted(CLAUDE_PROJECTS_DIR.rglob("*.jsonl"))


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of message dicts."""
    messages = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed JSONL line in %s", path)
    except OSError as e:
        logger.error("Failed to read session file %s: %s", path, e)
    return messages


def _session_id_from_path(path: Path) -> str:
    """Derive a session ID from a JSONL file path.

    Uses the path relative to the projects dir so IDs are stable
    and human-readable (e.g. 'myproject/session.jsonl').
    """
    try:
        return str(path.relative_to(CLAUDE_PROJECTS_DIR))
    except ValueError:
        return str(path)


def _path_from_session_id(session_id: str) -> Path:
    """Resolve a session ID back to an absolute path."""
    return CLAUDE_PROJECTS_DIR / session_id


def list_sessions() -> list[dict[str, Any]]:
    """Discover active Claude Code sessions.

    Returns a list of dicts with keys: id, last_modified, project_path, file.
    """
    sessions = []
    for path in _find_session_files():
        stat = path.stat()
        sid = _session_id_from_path(path)
        # Derive the project path from the directory structure
        project_path = str(path.parent.relative_to(CLAUDE_PROJECTS_DIR))
        sessions.append(
            {
                "id": sid,
                "last_modified": stat.st_mtime,
                "project_path": project_path,
                "file": str(path),
            }
        )
    # Most recently modified first
    sessions.sort(key=lambda s: s["last_modified"], reverse=True)
    logger.info("Found %d Claude sessions", len(sessions))
    return sessions


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read full conversation history from a session's JSONL file."""
    path = _path_from_session_id(session_id)
    if not path.exists():
        logger.error("Session file not found: %s", path)
        return []
    messages = _parse_jsonl(path)
    logger.info("Read %d messages from session %s", len(messages), session_id)
    return messages


def send_input(session_id: str, text: str) -> dict[str, Any]:
    """Send input to a Claude Code session via the CLI.

    MVP approach: shells out to `claude` with the message text.
    The session_id is used to determine the project directory context.
    """
    path = _path_from_session_id(session_id)
    work_dir = str(path.parent) if path.parent.is_dir() else str(Path.home())

    logger.info("Sending input to session %s: %s", session_id, text[:80])
    try:
        result = subprocess.run(
            ["claude", "--print", text],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        logger.error("claude CLI not found on PATH")
        return {"success": False, "error": "claude CLI not found"}
    except subprocess.TimeoutExpired:
        logger.error("claude CLI timed out for session %s", session_id)
        return {"success": False, "error": "CLI timeout"}
    except Exception as e:
        logger.error("Failed to send input: %s", e)
        return {"success": False, "error": str(e)}


async def tail_session(
    session_id: str,
    callback: Callable[[list[dict[str, Any]]], Coroutine],
) -> None:
    """Watch a session file for changes and invoke callback with new messages.

    Uses filesystem polling (mtime check every 500ms). Runs until cancelled.
    """
    path = _path_from_session_id(session_id)
    if not path.exists():
        logger.error("Cannot tail non-existent session: %s", path)
        return

    last_mtime = path.stat().st_mtime
    last_line_count = sum(1 for _ in open(path, "r", encoding="utf-8"))
    logger.info("Tailing session %s (starting at line %d)", session_id, last_line_count)

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            current_mtime = path.stat().st_mtime
            if current_mtime <= last_mtime:
                continue

            last_mtime = current_mtime
            # Read only new lines
            new_messages = []
            with open(path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < last_line_count:
                        continue
                    line = line.strip()
                    if line:
                        try:
                            new_messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

            # Update line count
            with open(path, "r", encoding="utf-8") as f:
                last_line_count = sum(1 for _ in f)

            if new_messages:
                logger.info(
                    "Session %s: %d new messages", session_id, len(new_messages)
                )
                await callback(new_messages)

        except OSError as e:
            logger.error("Error tailing session %s: %s", session_id, e)
        except asyncio.CancelledError:
            logger.info("Stopped tailing session %s", session_id)
            raise
