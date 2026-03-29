# ADR: Phase 2 — Write Path via `--print --resume` (#3)

## Status: ACCEPTED

## Context

The write path enables the mobile app to send user messages to existing Claude Code sessions. The previous ADR proposed a complex architecture using `--output-format stream-json`, `--verbose`, `--include-partial-messages`, tail suppression during writes, and chunk filtering. That approach is now superseded.

The **revised architecture** is dramatically simpler:

- Use `claude --print --resume <uuid> --permission-mode bypassPermissions "message"` (no stream-json, no partial messages, no verbose).
- The CLI runs to completion. Its stdout is discarded — we do not stream it to mobile.
- The existing `tail_session()` (polling JSONL every 500ms) handles ALL content delivery. It already detects new records the CLI appends to the JSONL and forwards them via `session_output`.
- Only two new signals are needed: `write_started` and `write_ended`.
- No de-duplication is needed because there is only one content delivery path (tail).
- The `interceptor/` directory is being renamed to `devcon_server/` as part of this issue.

This ADR addresses the five remaining architecture decisions.

---

## Decision 1: CLI timeout and process management

The `claude --print --resume` call is blocking (awaited via `asyncio.create_subprocess_exec` + `process.communicate()`). Agent responses with many tool uses can run for several minutes.

**Options:**

- **Option A: 300-second hard timeout, SIGTERM then SIGKILL** — Set `timeout=300` on `process.communicate()`. On `asyncio.TimeoutError`, send SIGTERM, wait 5s, then SIGKILL if still running. Not configurable in MVP.
  - Pros: Simple, prevents zombie processes, 5 minutes is generous for most agent turns.
  - Cons: Complex multi-tool-use turns (e.g., large refactors) could exceed 300s. Not configurable without code change.

- **Option B: 600-second timeout, configurable via environment variable** — Default 600s, overridable with `CLAUDE_WRITE_TIMEOUT_SECS`. Same SIGTERM/SIGKILL escalation.
  - Pros: 10 minutes covers nearly all realistic agent turns. Configurable without code change.
  - Cons: Slightly more code. Long timeout means a stuck process blocks the session longer.

- **Option C: No timeout (wait indefinitely)** — Rely on the CLI to terminate on its own.
  - Pros: Zero false kills.
  - Cons: A stuck CLI blocks the session forever. No way for mobile user to recover without server restart.

**Recommendation: Option B** — 600s default with `CLAUDE_WRITE_TIMEOUT_SECS` override. Agent turns involving many sequential tool uses (file edits, test runs, searches) routinely take 3-5 minutes. A 10-minute default avoids premature kills while still providing a safety net. The escalation sequence is:

1. `asyncio.TimeoutError` fires after the configured timeout.
2. Send `SIGTERM` to the process.
3. Wait up to 5 seconds for graceful exit.
4. If still alive, send `SIGKILL`.
5. Send `write_ended` with `success=false, error="CLI timeout after {N}s"`.

---

## Decision 2: `--permission-mode bypassPermissions`

The `--permission-mode bypassPermissions` flag tells the CLI to skip all tool-use permission prompts (file writes, command execution, etc.). Without it, `--print` mode would hang waiting for interactive approval that never comes.

**Options:**

- **Option A: Use `bypassPermissions` for MVP, document the risk**
  - Pros: Only viable option for non-interactive CLI use. The `--print` flag already implies non-interactive mode. The mobile user explicitly initiated the request. Within a devcontainer, blast radius is limited.
  - Cons: No per-tool approval granularity. A bad prompt could cause destructive file operations.

- **Option B: Use `--permission-mode allowedTools` with a curated list**
  - Pros: Finer-grained control.
  - Cons: The `allowedTools` format is undocumented / unstable. Maintaining a tool whitelist adds ongoing burden. Many useful agent actions would be blocked.

- **Option C: Use `acceptEdits` permission mode**
  - Pros: Middle ground — allows file edits but blocks shell commands.
  - Cons: Blocks `Bash` tool which is essential for most agent workflows (running tests, checking status, etc.).

**Recommendation: Option A** — `bypassPermissions` is the only practical choice for non-interactive `--print --resume`. The devcontainer provides sandboxing. Document as: "Mobile write path bypasses permission prompts. Use with the same trust level as the VSCode extension."

---

## Decision 3: Session cwd extraction strategy

The CLI `--resume` must run from a working directory where it can locate the session. Research shows the JSONL `cwd` field is present on every `user` and `assistant` record observed in real session files.

**Research findings:**

- Directory naming: `~/.claude/projects/-workspaces-hub-5-vsclaudemobile/` corresponds to cwd `/workspaces/hub_5/vsclaudemobile`. The encoding replaces `/` with `-` and prepends `-`.
- The `cwd` field on `user` records contains the exact original path (e.g., `/workspaces/hub_5/vsclaudemobile`).
- The `cwd` can change within a session (observed: same session has records with `/workspaces/hub_3` and `/workspaces/hub_3/tts_server`). The first `user` record's `cwd` is the project root.
- Directory name decoding is **ambiguous**: `-workspaces-hub-5-vsclaudemobile` — the hyphens could be path separators or literal hyphens. Reliable reverse-decoding would require filesystem existence probing.

**Options:**

- **Option A: JSONL `cwd` field primary, directory name fallback** _(recommended)_
  - Read the first `user` record from the JSONL file, return its `cwd` field.
  - Fallback if no `user` record found: replace leading `-` with `/`, then replace remaining `-` with `/`, check if path exists. If not, try common prefixes like `/workspaces/`.
  - Pros: `cwd` field is authoritative and always present on user records. Fast — scan stops at first `user` record.
  - Cons: Requires reading the JSONL file (minimal cost — first user record is typically within the first 5 lines).

- **Option B: Directory name decoding only**
  - Pros: No file I/O needed.
  - Cons: Ambiguous decoding. `-workspaces-hub-5-vsclaudemobile` could be `/workspaces/hub/5/vsclaudemobile` or `/workspaces/hub-5/vsclaudemobile` etc. Requires filesystem probing to disambiguate.

**Recommendation: Option A** — The `cwd` field is reliable, present on every observed `user` record, and authoritative. Implementation:

```python
def _extract_cwd(session_path: Path) -> str | None:
    """Read first user record's cwd field from JSONL."""
    with open(session_path) as f:
        for line in f:
            try:
                record = json.loads(line.strip())
                if record.get("type") == "user" and "cwd" in record:
                    return record["cwd"]
            except (json.JSONDecodeError, KeyError):
                continue
    return None
```

Fallback: use the parent directory of the session file (i.e., the `~/.claude/projects/<encoded-dir>/` path) as a last resort — this is not a valid cwd for the project but allows the CLI to at least attempt to run.

---

## Decision 4: Write signal semantics

Two signals bracket the write operation: `write_started` (sent immediately when the write begins) and `write_ended` (sent when the CLI process exits or times out).

**Options for `write_started` fields:**

- **Option A: Minimal** — `{type: "write_started", session_id: str}`
  - Pros: Simple. Mobile only needs to know "a write is in progress for this session."
  - Cons: No echo of the user's message text.

- **Option B: Include echo** — `{type: "write_started", session_id: str, text: str}`
  - Pros: Mobile can display "Sending: ..." or optimistic user bubble.
  - Cons: Mobile already has the text (it sent it). Minor redundancy.

**Recommendation for `write_started`: Option A (minimal).** Mobile already knows the text it sent and can show an optimistic user message bubble immediately. The signal's purpose is purely to trigger the "busy" UI state.

**Options for `write_ended` fields:**

- **Option A: Success/error only** — `{type: "write_ended", session_id: str, success: bool, error: str | null}`
  - Pros: Simple. Mobile needs to know: did it work, and if not, what went wrong.
  - Cons: No duration or cost metadata.

- **Option B: Include metadata** — `{type: "write_ended", session_id: str, success: bool, error: str | null, duration_ms: int | null, returncode: int | null}`
  - Pros: Duration useful for UX (show how long the response took). Return code useful for debugging.
  - Cons: Slightly more fields.

**Recommendation for `write_ended`: Option B (include metadata).** The `duration_ms` and `returncode` fields are trivially cheap to include (computed from the subprocess) and useful for both UX and debugging. The `error` field contains human-readable text (e.g., "CLI timeout after 600s", "claude CLI not found on PATH", stderr excerpt on non-zero exit).

**Final signal shapes:**

```
write_started: {type: "write_started", session_id: str}
write_ended:   {type: "write_ended", session_id: str, success: bool, error: str | null, duration_ms: int | null, returncode: int | null}
```

---

## Decision 5: Concurrent write guard scope

When a write is active for a session, additional write requests must be rejected to prevent interleaved CLI processes appending to the same JSONL.

**Options:**

- **Option A: Per-session guard** — `_active_writes: dict[str, asyncio.Task]` keyed by session_id. A second write to the same session is rejected. Writes to different sessions proceed concurrently.
  - Pros: Allows productive use of multiple sessions from mobile. Natural granularity.
  - Cons: Multiple concurrent CLI processes consume more resources.

- **Option B: Global guard (one write at a time)** — Single `_active_write: asyncio.Task | None`. Any write request while another is active is rejected.
  - Pros: Simplest. Minimal resource usage.
  - Cons: Unnecessarily blocks writes to other sessions. Poor UX if user switches sessions.

**Recommendation: Option A (per-session guard).** The primary risk is two CLI processes writing to the same JSONL. Different sessions have different JSONL files, so concurrent writes to different sessions are safe. The mobile user is unlikely to have many concurrent writes, so resource concerns are moot.

**Rejection message to mobile:**

```json
{
  "type": "write_ended",
  "session_id": "...",
  "success": false,
  "error": "A write is already in progress for this session. Please wait for it to complete.",
  "duration_ms": null,
  "returncode": null
}
```

Note: The rejection is sent as a `write_ended` (not a separate error type) so mobile only needs to handle one "write finished" code path. No `write_started` is sent for rejected writes.

---

## Consequences

### CLI command template

```
<CLAUDE_BINARY> --print --resume <uuid> --permission-mode bypassPermissions "<user message>"
```

Run with `cwd` extracted from the first `user` record's `cwd` field in the session JSONL.

### New WebSocket message types

| Type | Direction | Key fields |
|------|-----------|------------|
| `write_started` | devcon_server -> hub -> mobile | `session_id` |
| `write_ended` | devcon_server -> hub -> mobile | `session_id`, `success`, `error`, `duration_ms`, `returncode` |

### Content delivery

- Tail remains running throughout the write. No suppression, no restart needed.
- The CLI appends the user message and assistant response to the JSONL.
- `tail_session()` detects the new records and delivers them via `session_output`.
- No de-duplication is needed (single delivery path).

### Process lifecycle

1. Mobile sends `session_input` with `session_id` and `text`.
2. Devcon server checks per-session concurrency guard.
3. If busy, sends `write_ended` with rejection error (no `write_started`).
4. If free, sends `write_started`, spawns async CLI process.
5. CLI runs to completion (or timeout).
6. Sends `write_ended` with result metadata.
7. Meanwhile, `tail_session()` delivers content blocks as they appear in the JSONL.

### Known MVP limitations

- Concurrent VSCode + mobile use on the same session is unsupported. Primary use case is mobile-when-away-from-desk.
- `bypassPermissions` grants full tool access without per-action approval.
- 600s timeout may not cover exceptionally long agent turns (configurable via env var).

---

## Updated Acceptance Criteria

- [ ] CLI invocation uses `--print --resume <uuid> --permission-mode bypassPermissions`
- [ ] `cwd` extracted from first `user` record in JSONL, with fallback
- [ ] `write_started` signal sent on write begin (disables mobile input, shows spinner)
- [ ] `write_ended` signal sent on write completion with `success`, `error`, `duration_ms`, `returncode`
- [ ] Concurrent writes to same session rejected via per-session guard
- [ ] Rejection sent as `write_ended` with `success=false` (no `write_started` emitted)
- [ ] CLI timeout defaults to 600s, configurable via `CLAUDE_WRITE_TIMEOUT_SECS`
- [ ] Timeout escalation: SIGTERM, 5s grace, SIGKILL
- [ ] Errors (CLI not found, timeout, non-zero exit) surface to mobile via `write_ended`
- [ ] `tail_session()` remains running during writes (no suppression)
- [ ] Response content delivered to mobile via existing `session_output` path
- [ ] `interceptor/` renamed to `devcon_server/` throughout
- [ ] Hub WS endpoint renamed from `/ws/interceptor/` to `/ws/devcon/`
- [ ] Mobile `session_input` sends `text` field (not `content`)
- [ ] Mobile properly renders `session_output` with `messages` array and content blocks
