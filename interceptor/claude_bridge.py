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
    """Find top-level JSONL conversation files under ~/.claude/projects/.

    Only returns files directly inside project directories — excludes subagent
    files nested in subdirectories (e.g. <uuid>/subagents/*.jsonl).
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        logger.warning("Claude projects dir not found: %s", CLAUDE_PROJECTS_DIR)
        return []
    # Session files live at <project-dir>/<uuid>.jsonl — exactly one level deep
    return sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))


_KEPT_CONTENT_TYPES = {"text", "thinking", "tool_use", "tool_result"}


def _extract_content(content_blocks: Any) -> list[dict[str, Any]]:
    """Normalise a content block list, keeping known types and dropping unknowns."""
    if isinstance(content_blocks, str):
        return [{"type": "text", "text": content_blocks}] if content_blocks else []
    if not isinstance(content_blocks, list):
        return []
    result = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _KEPT_CONTENT_TYPES:
            result.append(block)
        else:
            logger.debug("Dropping unknown content block type: %s", block.get("type"))
    return result


def _build_message(raw: Any) -> dict[str, Any] | None:
    """Convert a raw JSONL record into a structured message dict.

    Returns None for non-user/assistant record types (e.g. queue-operation,
    file-history-snapshot, ai-title, last-prompt).
    """
    if not isinstance(raw, dict):
        return None
    record_type = raw.get("type")
    if record_type not in ("user", "assistant"):
        return None

    message = raw.get("message", {})
    if not isinstance(message, dict):
        return None

    role = message.get("role", record_type)
    content = _extract_content(message.get("content", []))

    msg: dict[str, Any] = {
        "role": role,
        "uuid": raw.get("uuid", ""),
        "timestamp": raw.get("timestamp", ""),
        "session_id": raw.get("sessionId", ""),
        "content": content,
        "model": None,
        "stop_reason": None,
    }

    if record_type == "assistant":
        msg["model"] = message.get("model")
        msg["stop_reason"] = message.get("stop_reason")

    return msg


def _extract_title(lines: list[Any]) -> str | None:
    """Scan a list of parsed JSONL records for the first ai-title entry."""
    for record in lines:
        if isinstance(record, dict) and record.get("type") == "ai-title":
            title = record.get("aiTitle")
            if title:
                return str(title)
    return None


def _parse_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a JSONL file into structured messages and an optional title.

    Returns a tuple of (messages, title) where messages contains only
    user/assistant records normalised through _build_message.
    """
    raw_records: list[Any] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        raw_records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed JSONL line in %s", path)
    except OSError as e:
        logger.error("Failed to read session file %s: %s", path, e)
        return [], None

    title = _extract_title(raw_records)
    messages = [m for r in raw_records if (m := _build_message(r)) is not None]
    return messages, title


def _extract_title_from_file(path: Path) -> str | None:
    """Scan a JSONL file for the first ai-title record.

    Used by list_sessions(). Reads the full file since ai-title records
    can appear at any position.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("type") == "ai-title":
                        title = record.get("aiTitle")
                        if title:
                            return str(title)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return None


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
    """Resolve a session ID back to an absolute path.

    Raises ValueError if the resolved path escapes CLAUDE_PROJECTS_DIR.
    """
    resolved = (CLAUDE_PROJECTS_DIR / session_id).resolve()
    if not str(resolved).startswith(str(CLAUDE_PROJECTS_DIR.resolve())):
        raise ValueError(f"session_id escapes projects dir: {session_id!r}")
    return resolved


def list_sessions() -> list[dict[str, Any]]:
    """Discover active Claude Code sessions.

    Returns a list of dicts with keys: id, last_modified, project_path, file, title.
    """
    sessions = []
    for path in _find_session_files():
        stat = path.stat()
        sid = _session_id_from_path(path)
        # Derive the project path from the directory structure
        project_path = str(path.parent.relative_to(CLAUDE_PROJECTS_DIR))
        title = _extract_title_from_file(path)
        sessions.append(
            {
                "id": sid,
                "last_modified": stat.st_mtime,
                "project_path": project_path,
                "file": str(path),
                "title": title,
            }
        )
    # Most recently modified first
    sessions.sort(key=lambda s: s["last_modified"], reverse=True)
    logger.info("Found %d Claude sessions", len(sessions))
    return sessions


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read full conversation history from a session's JSONL file."""
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: %s", session_id)
        return []
    if not path.exists():
        logger.error("Session file not found: %s", path)
        return []
    messages, _ = _parse_jsonl(path)
    logger.info("Read %d messages from session %s", len(messages), session_id)
    return messages


def send_input(session_id: str, text: str) -> dict[str, Any]:
    """Send input to a Claude Code session via the CLI.

    MVP approach: shells out to `claude` with the message text.
    The session_id is used to determine the project directory context.
    """
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: %s", session_id)
        return {"success": False, "error": "Invalid session ID"}
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
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: %s", session_id)
        return
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
            # Read only new lines, tracking total count in-loop
            new_messages = []
            current_line_count = 0
            with open(path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    current_line_count = i + 1
                    if i < last_line_count:
                        continue
                    line = line.strip()
                    if line:
                        try:
                            raw = json.loads(line)
                            msg = _build_message(raw)
                            if msg is not None:
                                new_messages.append(msg)
                        except json.JSONDecodeError:
                            pass
            last_line_count = current_line_count

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
