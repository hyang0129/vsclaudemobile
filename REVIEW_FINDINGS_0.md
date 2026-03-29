# Review Findings: PR #4 — fix(#3): Implement write path and rename interceptor to devcon_server

**Branch**: fix/issue-3-interceptor-write-path
**Date**: 2026-03-29
**Reviewer**: Agent (read-only analysis)

**Files reviewed**: `devcon_server/__init__.py`, `devcon_server/__main__.py`, `devcon_server/claude_bridge.py`, `devcon_server/client.py`, `hub_server/app.py`, `mobile/index.html`, `scripts/test_read_live.py`, `scripts/test_write_live.py`, `CLAUDE.md`, `ISSUE_3_PLAN.md`, `ISSUE_3_ADR.md`

---

## Summary

Solid implementation. The rename from interceptor to devcon_server is thorough (no stale references in active code -- only historical plan/review documents retain "interceptor" which is expected). The write path correctly follows all five ADR decisions: 600s configurable timeout with SIGTERM/SIGKILL escalation, bypassPermissions, cwd from JSONL, minimal write_started / metadata-rich write_ended, per-session concurrency guard. The previously-caught issues (writeActive guard in sendMessage, node prefix for .js binary) are properly fixed.

Two major findings relate to the mobile UI rendering the wrong field names from the API, which breaks the navigation flow.

---

### [SEVERITY: major] Mobile session list renders wrong field names -- titles never shown
**ID**: F-1
**File**: `mobile/index.html:569-570,584`
**Problem**: `renderSessions()` reads `s.name` and `s.status`, but the devcon server's `list_sessions()` returns objects with fields `id`, `title`, `project_path`, `file`, `last_modified`. There is no `name` or `status` field. As a result, session cards always fall back to `s.id` (e.g., `-workspaces-hub-5-vsclaudemobile/abc-def-123.jsonl`) and the subtitle always shows the fallback string "session". The `selectSession()` function at line 584 similarly uses `s.name || s.id` and will never show the session title. This makes it difficult for users to identify which session to select.
**Suggested fix**: Change `s.name` to `s.title` in `renderSessions()` (line 569) and `selectSession()` (line 584). Optionally display `s.project_path` in the card-sub line instead of `s.status`.
**Decision required**: no
**Parallelizable**: yes
**Conflicts with**: none

---

### [SEVERITY: major] Mobile window list receives strings from API but expects objects
**ID**: F-2
**File**: `mobile/index.html:512,529-541` and `hub_server/app.py:180`
**Problem**: `/api/windows` returns `{"windows": ["hostname1", ...]}` -- a list of plain strings. But `renderWindows()` accesses `w.name`, `w.id`, and `w.status` on each element, and `selectWindow()` sets `selectedWindowId = w.id`. Since JavaScript strings do not have an `.id` property, `selectedWindowId` becomes `undefined`. This causes the mobile WebSocket URL to be `/ws/mobile/undefined` and the session list request to go to `/api/windows/undefined/sessions`, breaking the entire navigation from windows to sessions.
**Suggested fix**: Either (a) change `list_windows()` in `hub_server/app.py` to return objects: `[{"id": wid, "name": wid} for wid in manager.devcons.keys()]`, or (b) update `renderWindows` to handle strings by wrapping them: `const id = typeof w === 'string' ? w : w.id`.
**Decision required**: no (pick either approach)
**Parallelizable**: yes
**Conflicts with**: none

---

### [SEVERITY: minor] Active write tasks not cancelled on WebSocket disconnect
**ID**: F-3
**File**: `devcon_server/client.py:218-220`
**Problem**: When the hub connection drops, `_cancel_tail()` cancels tail tasks in the `finally` block, but `_active_writes` tasks are not cancelled. The CLI subprocess continues running for up to 600 seconds consuming resources. When `_execute_write` completes, its `_send()` call silently fails (because `self._ws is None`), so the `write_ended` message is lost. If the mobile client reconnects, it has no way to know whether a write completed or failed during the disconnect. The `_active_writes` entry is eventually cleaned up by the `finally` block in `_execute_write`, so this won't permanently block new writes.
**Suggested fix**: Add a `_cancel_active_writes()` method alongside `_cancel_tail()` in the `finally` block of `run()`. Cancel each task and, if needed, terminate the subprocess. Alternatively, document this as a known MVP limitation.
**Decision required**: no
**Parallelizable**: yes
**Conflicts with**: none

---

### [SEVERITY: minor] Tail log message shows identical start and end line numbers
**ID**: F-4
**File**: `devcon_server/claude_bridge.py:420-422`
**Problem**: The log message `"Session {}: {} new messages (line {} -> {})"` uses `last_line_count` and `current_line_count`. However, `last_line_count` is updated to `current_line_count` at line 417 before this log executes. Both values are always identical, producing misleading output like "line 50 -> 50" instead of the intended "line 45 -> 50".
**Suggested fix**: Save the old count before updating: add `old_line_count = last_line_count` before line 412, then use `old_line_count` in the log message at line 421.
**Decision required**: no
**Parallelizable**: yes
**Conflicts with**: none

---

### [SEVERITY: minor] No validation that extracted cwd directory exists before subprocess launch
**ID**: F-5
**File**: `devcon_server/claude_bridge.py:294-299`
**Problem**: `_extract_cwd()` returns the `cwd` field from the JSONL, which is passed directly to `create_subprocess_exec(cwd=cwd)`. If the directory was deleted, renamed, or the session was from another machine, the subprocess will fail with `FileNotFoundError` or `NotADirectoryError`. The outer `except Exception` at line 359 catches this, but the error message "Failed to send input..." doesn't indicate the root cause is a bad working directory, making debugging harder.
**Suggested fix**: Add an `os.path.isdir(cwd)` check after extracting cwd. If invalid, fall back to `str(path.parent)` and log a warning. This makes the fallback chain explicit and the error messages actionable.
**Decision required**: no
**Parallelizable**: yes
**Conflicts with**: none

---

### [SEVERITY: minor] Session switch does not cancel active writes for the previous session
**ID**: F-6
**File**: `devcon_server/client.py:58-86`
**Problem**: When the user selects a new session, `_cancel_tail()` stops tailing the old session, but any in-progress write for the old session continues running. The eventual `write_ended` for the old session will be sent to the mobile client, which has moved on to viewing a different session. This could cause confusion in the mobile UI (e.g., briefly showing an error for a session the user is no longer viewing).
**Suggested fix**: In `_handle_session_select`, cancel active writes for sessions other than the newly selected one, or filter `write_ended` messages in the mobile UI by checking `data.session_id === selectedSessionId`.
**Decision required**: no
**Parallelizable**: yes
**Conflicts with**: F-3

---

### [SEVERITY: minor] hub_server uses stdlib logging instead of loguru (pre-existing)
**ID**: F-7
**File**: `hub_server/app.py:4-8`
**Problem**: CLAUDE.md mandates loguru for all logging, but hub_server uses stdlib `logging`. This is pre-existing from the initial scaffold -- this PR only renamed variables. Noting it here since the PR touches this file extensively and it would be a natural time to fix it.
**Suggested fix**: Replace `import logging` with `from loguru import logger` and update format strings from `%s` to `{}`. Not required for this PR.
**Decision required**: yes (scope question -- fix now or defer?)
**Parallelizable**: yes
**Conflicts with**: none

---

## Scope-creep check

| Change | In scope for #3? |
|--------|-------------------|
| `interceptor/` -> `devcon_server/` rename | Yes -- explicitly part of this issue |
| Hub WS endpoint `/ws/interceptor/` -> `/ws/devcon/` | Yes -- part of rename |
| `send_input` -> `send_input_async` with `--print --resume` | Yes -- core of the write path |
| `_find_claude_binary` (VSCode extension path discovery) | Yes -- needed for CLI invocation in devcontainer |
| `_extract_cwd`, `_extract_session_uuid` | Yes -- needed by write path |
| Timeout, SIGTERM/SIGKILL escalation | Yes -- per ADR Decision 1 |
| Per-session concurrency guard | Yes -- per ADR Decision 5 |
| `write_started`/`write_ended` signals | Yes -- per ADR Decision 4 |
| Mobile `session_output` rendering overhaul | Yes -- part of the issue's acceptance criteria |
| Mobile `writeActive` guard, disabled input | Yes -- write signal handling |
| `scripts/test_write_live.py` | Yes -- integration test for write path |
| CLAUDE.md updates | Yes -- documentation for rename and new message types |

No scope creep detected. All changes directly serve the issue's two objectives (rename + write path).

---

## Verdict

| Severity | Count |
|----------|-------|
| Critical | 0 |
| Major | 2 |
| Minor | 5 |

**F-1 and F-2 must be fixed before merge**: they break the mobile navigation flow. F-2 (windows rendered as strings) prevents users from reaching the session list at all. F-1 (wrong field name for session title) degrades session selection UX. Both are straightforward field-name fixes.

**F-3 through F-7 can be deferred** to post-merge follow-ups. They relate to edge cases (disconnect during write, session switching), logging cosmetics, and a pre-existing logging framework mismatch.
