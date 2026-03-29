# Intent Validation

## Original PR Intent

PR #4 (fix/issue-3-interceptor-write-path) implements the write path for mobile-to-Claude-Code session input. Key goals:

1. Mobile can send messages to active Claude Code sessions via `claude --print --resume`
2. `write_started`/`write_ended` signals manage UI state
3. Concurrent writes to the same session are rejected
4. Rename `interceptor/` to `devcon_server/` throughout
5. Fix mobile rendering to use `messages` array and content blocks
6. Fix `session_input` to send `text` field (not `content`)

## Analysis

### `devcon_server/claude_bridge.py`

**Original commit**: New file implementing `send_input_async`, `tail_session`, session discovery, message parsing.

**Fix cycle changes** (2 changes):

1. **cwd validation hardening** (line 295): Added `or not os.path.isdir(cwd)` to the fallback condition, plus a warning log when the extracted cwd directory does not exist. This strictly strengthens the original guard -- the original only checked `cwd is None`, the fix also handles the case where the JSONL records a cwd that no longer exists on disk. **Intent preserved, guard strengthened.**

2. **Truncation detection logging fix** (lines 414-425): Added `prev_line_count = last_line_count` before the truncation check, then used `prev_line_count` in the log message. The original code logged `last_line_count` in the "new messages" info line, but `last_line_count` had already been mutated by the truncation branch above it, so the log would show incorrect values after a truncation event. The actual data flow (which lines get read, what gets sent to the callback) is unchanged -- this is purely a logging accuracy fix. **Intent preserved, bug fixed.**

### `devcon_server/client.py`

**Original commit**: New file implementing `DevconClient` with session list/select/input handlers, tail task management, concurrent write guards.

**Fix cycle changes** (2 changes):

1. **Cancel writes on session switch** (line 81): Added `self._cancel_active_writes(session_id)` in `_handle_session_select`. The original code cancelled tail tasks on session switch but forgot to cancel active write tasks. This is a resource leak / orphan process fix -- the write subprocess would keep running for the old session with no one listening for the result. **Intent preserved, missing cleanup added.**

2. **Cancel writes on disconnect** (line 234): Added `self._cancel_active_writes()` in the `finally` block of `run()`. Same rationale -- on disconnect, outstanding write subprocesses should be terminated. The original already cancelled tail tasks here. **Intent preserved, parallel cleanup added.**

3. **New `_cancel_active_writes` method** (lines 178-188): Mirrors the existing `_cancel_tail` pattern exactly. Takes an optional `keep_session_id`, cancels all others. **Consistent with existing design.**

### `hub_server/app.py`

**Original commit**: Renamed endpoint from `/ws/interceptor/` to `/ws/devcon/`, updated connection manager class names.

**Fix cycle change**: Changed `/api/windows` response from `list(manager.devcons.keys())` (list of strings) to `[{"id": wid, "name": wid} for wid in manager.devcons.keys()]` (list of dicts). This makes the windows endpoint return objects with `id` and `name` fields, which is more consistent with how the mobile app renders lists (cards expect object properties). **Intent preserved, API shape improved for mobile consumption.**

### `mobile/index.html`

**Original commit**: Added content block rendering, write_started/write_ended handling, input field sending `text`.

**Fix cycle changes**: Changed session card rendering from `s.name` to `s.title` and `s.status` to `s.project_path`. The backend `list_sessions()` returns `{id, last_modified, project_path, file, title}` -- there is no `name` or `status` field. The original mobile code referenced fields that don't exist in the API response, so session cards would have shown `undefined` or fallen back to `s.id` for every field. **Intent preserved, field names corrected to match actual API contract.**

## Findings

No intent risks detected.

All four fix-cycle changes are strictly additive or corrective:

- **No ordering reversals**: The truncation detection logic, tail cancellation, and write cancellation all execute in the same order as the original design intended.
- **No guard removals**: The cwd guard was strengthened (added `os.path.isdir` check), not weakened.
- **No logic inversions**: All boolean conditions maintain their original polarity.
- **No dead code introduced**: The new `_cancel_active_writes` method is called from two sites.
- **No config/env neutralisation**: The `CLAUDE_WRITE_TIMEOUT_SECS` env var, `bypassPermissions` mode, and SIGTERM escalation are untouched.

The fix commits correctly addressed: a missing cleanup path (orphan write tasks), a logging inaccuracy (truncation line count), a robustness gap (non-existent cwd directory), a field name mismatch (mobile referencing nonexistent API fields), and an API shape inconsistency (windows endpoint returning strings instead of objects).
