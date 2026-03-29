# Review: Issue #1 — Phase 1 Interceptor read-only session path

**Branch**: `fix/issue-1-read-claude-sessions`
**Files reviewed**: `interceptor/claude_bridge.py` (unstaged working-tree changes)
**Date**: 2026-03-29

---

## Summary

The implementation is largely correct and well-structured. The core parsing
pipeline (`_extract_content` → `_build_message` → `_parse_jsonl`) is sound.
Three issues need attention before merge: one major spec deviation (missing
`session_id` field in message dicts), one major correctness bug in
`_extract_title_from_file` (the `ai-title` record typically appears late in a
session file, not in the first 20 lines, making the title almost always
`None`), and one minor security issue (path traversal via session_id).

---

## Findings

### [SEVERITY: major] `_build_message` omits the `session_id` field required by the plan spec

**File**: `interceptor/claude_bridge.py:68-81`

**Problem**: The task spec (ISSUE_1_PLAN.md line 44) requires each message dict
to include `"session_id": str  # from raw["sessionId"] if present`. The
current implementation builds the message dict without this field. Real Claude
CLI JSONL records include a `sessionId` key; downstream consumers (mobile app,
tests) that rely on per-message session context will receive dicts missing an
expected field, which will cause `KeyError` or silent data loss.

**Fix**: Add `"session_id": raw.get("sessionId", "")` to the `msg` dict inside
`_build_message`:
```python
msg: dict[str, Any] = {
    "role": role,
    "uuid": raw.get("uuid", ""),
    "timestamp": raw.get("timestamp", ""),
    "session_id": raw.get("sessionId", ""),   # <-- add this
    "content": content,
    "model": None,
    "stop_reason": None,
}
```

---

### [SEVERITY: major] `_extract_title_from_file` scans only the first 20 lines, but `ai-title` records appear late in real session files

**File**: `interceptor/claude_bridge.py:119-142`

**Problem**: The function breaks after reading 20 lines (`if i >= max_lines:
break`). In real Claude CLI JSONL files the `ai-title` record is written by
the CLI after the first assistant response, meaning it appears at line 3–6 at
the absolute earliest and often later. For sessions with any preamble or for
sessions where the title was generated after an initial back-and-forth, it will
fall beyond line 20 and `list_sessions()` will always return `title: None`.

The ISSUE_1_PLAN notes (line 79) acknowledges that reading the full file is
acceptable for MVP: *"reading every file on every session_list call is
acceptable for the MVP given the small number of sessions expected"*. The
optimisation to scan only the first N lines is premature and incorrect — the
`ai-title` record position is not bounded.

**Fix**: Either remove the `max_lines` limit entirely (scan the whole file, as
`_parse_jsonl` does), or increase `max_lines` to a much larger default (e.g.
500). If a partial scan is kept for performance, it should scan from the
**end** of the file (last N lines), since `ai-title` entries are appended.
Simplest correct fix:

```python
def _extract_title_from_file(path: Path) -> str | None:
    """Scan a JSONL file for the first ai-title record."""
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
```

---

### [SEVERITY: major] Path traversal via `session_id` in `_path_from_session_id`

**File**: `interceptor/claude_bridge.py:157-159`

**Problem**: `_path_from_session_id` does no validation:
```python
def _path_from_session_id(session_id: str) -> Path:
    return CLAUDE_PROJECTS_DIR / session_id
```
A caller supplying `session_id = "../../etc/passwd"` would resolve to
`~/.claude/projects/../../etc/passwd` which normalises to `~/.etc/passwd` (one
level), or with deeper traversal to arbitrary paths on the system. `read_session`
and `tail_session` both call this function and then open the resolved path.
Because `session_id` values originate from the hub (and ultimately from the
mobile app over a network connection), this is a real attack surface once the
hub is reachable from mobile.

**Fix**: Resolve the path and assert it is still under `CLAUDE_PROJECTS_DIR`:
```python
def _path_from_session_id(session_id: str) -> Path:
    resolved = (CLAUDE_PROJECTS_DIR / session_id).resolve()
    if not str(resolved).startswith(str(CLAUDE_PROJECTS_DIR.resolve())):
        raise ValueError(f"session_id escapes projects dir: {session_id!r}")
    return resolved
```
Callers (`read_session`, `tail_session`) should catch `ValueError` and return
early with an error log.

---

### [SEVERITY: minor] `_extract_content` silently drops unknown block types without logging

**File**: `interceptor/claude_bridge.py:34-46`

**Problem**: The docstring and the plan spec (ISSUE_1_PLAN.md line 33) state
that unknown block types should be *dropped and logged at DEBUG level*. The
implementation drops them silently with no log call. This makes it impossible
to detect new block types introduced by a future Claude CLI version during
development or testing.

**Fix**: Add a DEBUG log for dropped blocks:
```python
for block in content_blocks:
    if not isinstance(block, dict):
        continue
    if block.get("type") in _KEPT_CONTENT_TYPES:
        result.append(block)
    else:
        logger.debug("Dropping unknown content block type: %s", block.get("type"))
```

---

### [SEVERITY: minor] `list_sessions` docstring does not mention the new `title` field

**File**: `interceptor/claude_bridge.py:162-166`

**Problem**: The docstring says *"Returns a list of dicts with keys: id,
last_modified, project_path, file."* — `title` is now a fifth key but is not
listed. Callers reading only the docstring will not know the field exists.

**Fix**: Update the docstring to:
```
Returns a list of dicts with keys: id, last_modified, project_path, file, title.
```

---

### [SEVERITY: minor] `tail_session` opens the file twice on every poll cycle

**File**: `interceptor/claude_bridge.py:261-278`

**Problem**: After reading new messages, the file is opened a second time just
to recount lines:
```python
with open(path, "r", encoding="utf-8") as f:
    last_line_count = sum(1 for _ in f)
```
This creates a TOCTOU window: if new lines are appended between the first and
second open, the updated `last_line_count` will skip those lines on the next
iteration. The correct pattern is to track the line count inside the first
read loop (increment a counter as lines are enumerated), not to re-open the
file.

**Fix**: Replace the second open with an in-loop counter:
```python
current_line_count = last_line_count
with open(path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        current_line_count = i + 1
        if i < last_line_count:
            continue
        ...
last_line_count = current_line_count
```

---

### [SEVERITY: minor] `_build_message` returns empty-content messages without warning

**File**: `interceptor/claude_bridge.py:49-81`

**Problem**: If a `user` or `assistant` record has an empty or unrecognised
`content` list (e.g. all blocks are of unknown type), `_build_message` returns
a valid dict with `"content": []`. The message is forwarded to the mobile app
as a real message with no content. This can happen with tool-result-only user
turns where all blocks are of an unrecognised type. While not a crash, it
produces confusing empty chat bubbles on the mobile side.

**Fix**: Either drop messages with empty content (return `None`) or log a
DEBUG warning when content is empty after filtering. A warning is preferable to
silently dropping a message that the user sent:
```python
if not content:
    logger.debug("Message %s has no recognised content blocks after filtering",
                 raw.get("uuid", "?"))
```

---

## Acceptance Criteria Checklist (from ISSUE_1_PLAN.md)

| Criterion | Status |
|-----------|--------|
| `list_sessions()` returns `title` field | Partially met — field present but often `None` due to 20-line scan limit |
| `read_session()` returns `role`, `uuid`, `timestamp`, `content` | Met — but `session_id` field is missing (spec requires it) |
| Content items are valid typed blocks | Met |
| `tail_session()` callback receives only structured dicts | Met |
| `client.py` requires no changes | Met |
| No Python exceptions against real `~/.claude/projects/` | Likely met for happy path |
