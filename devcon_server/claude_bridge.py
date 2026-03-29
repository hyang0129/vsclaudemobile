"""Interface to Claude Code CLI and session data."""

import asyncio
import glob as globmod
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
POLL_INTERVAL = 0.5  # seconds
WRITE_TIMEOUT_SECS = int(os.environ.get("CLAUDE_WRITE_TIMEOUT_SECS", "600"))


def _find_session_files() -> list[Path]:
    """Find top-level JSONL conversation files under ~/.claude/projects/.

    Only returns files directly inside project directories — excludes subagent
    files nested in subdirectories (e.g. <uuid>/subagents/*.jsonl).
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        logger.warning("Claude projects dir not found: {}", CLAUDE_PROJECTS_DIR)
        return []
    # Session files live at <project-dir>/<uuid>.jsonl — exactly one level deep
    files = sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))
    logger.debug("Found {} JSONL session files in {}", len(files), CLAUDE_PROJECTS_DIR)
    return files


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
            logger.debug("Dropping unknown content block type: {}", block.get("type"))
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
        logger.trace("Skipping JSONL record type={}", record_type)
        return None

    message = raw.get("message", {})
    if not isinstance(message, dict):
        logger.warning("Record type={} has non-dict message field, skipping", record_type)
        return None

    role = message.get("role", record_type)
    content = _extract_content(message.get("content", []))
    logger.trace("Built message: role={}, uuid={}, content_blocks={}", role, raw.get("uuid", "?")[:12], len(content))

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
                        logger.debug("Skipping malformed JSONL line in {}", path)
    except OSError as e:
        logger.error("Failed to read session file {}: {}", path, e)
        return [], None

    title = _extract_title(raw_records)
    messages = [m for r in raw_records if (m := _build_message(r)) is not None]
    logger.debug("Parsed {}: {} raw records → {} messages, title={!r}", path.name, len(raw_records), len(messages), title)
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
    projects_resolved = CLAUDE_PROJECTS_DIR.resolve()
    if projects_resolved not in resolved.parents and resolved != projects_resolved:
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
    titled_count = sum(1 for s in sessions if s.get("title"))
    logger.info("Found {} Claude sessions ({} with titles)", len(sessions), titled_count)
    return sessions


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read full conversation history from a session's JSONL file."""
    logger.debug("read_session called: session_id={}", session_id)
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: {}", session_id)
        return []
    if not path.exists():
        logger.error("Session file not found: {}", path)
        return []
    logger.debug("Reading session file: {}", path)
    messages, _ = _parse_jsonl(path)
    logger.info("Read {} messages from session {}", len(messages), session_id)
    return messages


def _extract_session_uuid(session_id: str) -> str:
    """Extract the UUID stem from a session ID (e.g. 'project/abc-123.jsonl' -> 'abc-123')."""
    return Path(session_id).stem


def _extract_cwd(session_path: Path) -> str | None:
    """Read the JSONL file and return the cwd from the first 'user' record, or None."""
    logger.debug("Extracting cwd from {}", session_path)
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") == "user":
                    cwd = record.get("cwd")
                    if cwd:
                        logger.debug("Found cwd={!r} in first user record", cwd)
                        return str(cwd)
    except OSError as e:
        logger.warning("Could not read session file for cwd extraction: {}", e)
    logger.debug("No cwd found in session file {}", session_path)
    return None


def _find_claude_binary() -> str:
    """Locate the claude CLI binary, checking VSCode extension paths first."""
    # Check devcontainer VSCode server paths
    pattern = str(
        Path.home()
        / ".vscode-server"
        / "cli"
        / "servers"
        / "*"
        / "server"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "cli.js"
    )
    matches = sorted(globmod.glob(pattern))
    if matches:
        binary = matches[-1]  # latest server version
        logger.debug("Found claude binary at VSCode extension path: {}", binary)
        return binary

    # Fallback: assume claude is on PATH
    logger.debug("No VSCode extension binary found, falling back to 'claude' on PATH")
    return "claude"


async def send_input_async(session_id: str, text: str) -> dict[str, Any]:
    """Send input to a Claude Code session via the CLI (async).

    Resumes the session identified by session_id using `claude --print --resume <uuid>`.
    Returns a dict with success, returncode, stderr, and optionally error.
    """
    logger.debug("send_input_async called: session_id={}, text_len={}", session_id, len(text))
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: {}", session_id)
        return {"success": False, "error": "Invalid session ID", "returncode": None}

    uuid = _extract_session_uuid(session_id)
    logger.debug("Extracted UUID: {}", uuid)

    cwd = _extract_cwd(path)
    if cwd is None:
        cwd = str(path.parent)
        logger.debug("Using fallback cwd from session path parent: {}", cwd)
    else:
        logger.debug("Using cwd from session file: {}", cwd)

    claude_binary = _find_claude_binary()
    if claude_binary.endswith(".js"):
        cmd = ["node", claude_binary, "--print", "--resume", uuid, "--permission-mode", "bypassPermissions", text]
    else:
        cmd = [claude_binary, "--print", "--resume", uuid, "--permission-mode", "bypassPermissions", text]
    logger.info("Sending input to session {} (uuid={}): {!r}", session_id, uuid, text[:80])
    logger.debug("Command: {}", cmd)
    logger.debug("Working directory: {}", cwd)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        logger.debug("Subprocess started, pid={}", process.pid)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=WRITE_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            logger.warning("Claude CLI timed out after {}s for session {}, sending SIGTERM",
                           WRITE_TIMEOUT_SECS, session_id)
            try:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                    logger.debug("Process terminated gracefully after SIGTERM")
                except asyncio.TimeoutError:
                    logger.warning("Process did not terminate after SIGTERM, sending SIGKILL")
                    process.kill()
                    await process.wait()
            except ProcessLookupError:
                logger.debug("Process already exited before signal could be sent")
            return {
                "success": False,
                "error": f"CLI timeout after {WRITE_TIMEOUT_SECS}s",
                "returncode": None,
            }

        returncode = process.returncode
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        logger.info("Claude CLI exited: returncode={}, stderr_len={}", returncode, len(stderr_text))
        if stderr_text:
            logger.debug("stderr: {}", stderr_text[:500])

        return {
            "success": returncode == 0,
            "returncode": returncode,
            "stderr": stderr_text,
        }

    except FileNotFoundError:
        logger.error("Claude CLI not found: {}", claude_binary)
        return {"success": False, "error": f"claude CLI not found: {claude_binary}", "returncode": None}
    except Exception as e:
        logger.error("Failed to send input to session {}: {}", session_id, e)
        return {"success": False, "error": str(e), "returncode": None}


async def tail_session(
    session_id: str,
    callback: Callable[[list[dict[str, Any]]], Coroutine],
) -> None:
    """Watch a session file for changes and invoke callback with new messages.

    Uses filesystem polling (mtime check every 500ms). Runs until cancelled.
    """
    logger.debug("tail_session called: session_id={}", session_id)
    try:
        path = _path_from_session_id(session_id)
    except ValueError:
        logger.error("Invalid session_id: {}", session_id)
        return
    if not path.exists():
        logger.error("Cannot tail non-existent session: {}", path)
        return

    last_mtime = path.stat().st_mtime
    with open(path, "r", encoding="utf-8") as f:
        last_line_count = sum(1 for _ in f)
    logger.info("Tailing session {} (starting at line {}, file={})", session_id, last_line_count, path)

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
            # File was truncated/recreated — reset so next poll reads from start
            if current_line_count < last_line_count:
                logger.info("Session {} file truncated (was {} lines, now {}), resetting",
                            session_id, last_line_count, current_line_count)
                last_line_count = 0
            else:
                last_line_count = current_line_count

            if new_messages:
                logger.info(
                    "Session {}: {} new messages (line {} → {})",
                    session_id, len(new_messages), last_line_count, current_line_count,
                )
                for m in new_messages:
                    text_preview = ""
                    for c in m.get("content", []):
                        if c.get("type") == "text":
                            text_preview = c.get("text", "")[:100]
                            break
                    logger.debug("  new msg: role={}, uuid={}, preview={!r}",
                                 m["role"], m["uuid"][:12], text_preview)
                await callback(new_messages)
            else:
                logger.trace("Session {} mtime changed but no new user/assistant messages", session_id)

        except OSError as e:
            logger.error("Error tailing session {}: {}", session_id, e)
        except asyncio.CancelledError:
            logger.info("Stopped tailing session {}", session_id)
            raise
