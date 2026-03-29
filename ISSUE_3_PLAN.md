# Plan: Phase 2 — Interceptor write path / send user input to Claude Code sessions (#3)

## Summary

This issue has two parts: (1) rename the `interceptor/` component to `devcon_server/` throughout the codebase, and (2) implement the write path so mobile users can send messages to Claude Code sessions via `claude --print --resume <uuid> --permission-mode bypassPermissions`. The revised architecture is dramatically simpler than the original plan — no streaming JSON, no chunk filtering, no tail suppression. The CLI writes blocks incrementally to the session JSONL, and the existing `tail_session()` polling picks them up and delivers them to mobile via the existing `session_output` path. Only two new signals are needed: `write_started` and `write_ended`.

## Affected Files

| File | Change type | Owned by |
|---|---|---|
| `interceptor/` (entire directory) | rename to `devcon_server/` | Agent A |
| `devcon_server/__main__.py` | modify (update module refs, log file name) | Agent A |
| `devcon_server/client.py` | modify (rename class, update write handler, add concurrency guard, add signals) | Agent B |
| `devcon_server/claude_bridge.py` | modify (replace `send_input()` with async version using `--resume`, add `_extract_cwd`, `_extract_session_uuid`) | Agent B |
| `hub_server/app.py` | modify (rename WS endpoint `/ws/interceptor/` → `/ws/devcon/`, rename vars/methods/logs) | Agent A |
| `mobile/index.html` | modify (handle `write_started`/`write_ended`, fix `session_output` rendering, fix `content` vs `text` bug, add spinner) | Agent C |
| `CLAUDE.md` | modify (rename interceptor refs, update message types, update commands) | Agent A |
| `scripts/test_read_live.py` | modify (update import from `interceptor` to `devcon_server`) | Agent A |
| `scripts/test_write_live.py` | create (integration test for write path) | Agent D |

## File Ownership Table

| Agent | Files |
|---|---|
| Agent A (Rename) | `interceptor/` → `devcon_server/`, `devcon_server/__main__.py`, `hub_server/app.py`, `CLAUDE.md`, `scripts/test_read_live.py` |
| Agent B (Write Path Backend) | `devcon_server/claude_bridge.py`, `devcon_server/client.py` |
| Agent C (Mobile UI) | `mobile/index.html` |
| Agent D (Test) | `scripts/test_write_live.py` |

## Task List

### Wave 1 (parallel — rename + mobile UI proceed independently)

- **Task 1.1: Rename `interceptor/` to `devcon_server/` across codebase** — Agent A — files: `interceptor/` directory, `hub_server/app.py`, `CLAUDE.md`, `scripts/test_read_live.py`

  1. `git mv interceptor/ devcon_server/`
  2. In `devcon_server/__main__.py`: update import, description, log file name, log messages.
  3. In `hub_server/app.py`: rename WS endpoint `/ws/interceptor/{window_id}` → `/ws/devcon/{window_id}`. Rename `connect_interceptor` → `connect_devcon`, `disconnect_interceptor` → `disconnect_devcon`, `get_interceptor` → `get_devcon`, `self.interceptors` → `self.devcons`. Update all log messages.
  4. In `devcon_server/client.py`: rename class `InterceptorClient` → `DevconClient`. Update `ws_url` to `/ws/devcon/`. Update log messages.
  5. In `CLAUDE.md`: replace interceptor references. Update commands section.
  6. In `scripts/test_read_live.py`: update `from interceptor import claude_bridge` → `from devcon_server import claude_bridge`.

- **Task 1.2: Update mobile UI for write signals and fix rendering** — Agent C — files: `mobile/index.html`

  1. Fix `sendMessage()`: change `content: text` to `text: text` in `session_input` message.
  2. Fix `handleWsMessage()` for `session_output`: render `data.messages` array with content blocks (handle `full_history: true` vs `false`).
  3. Add `renderContentBlocks(blocks)` helper for text, thinking, tool_use, tool_result.
  4. Add `write_started` handler: disable input, show spinner.
  5. Add `write_ended` handler: re-enable input, show error if `success === false`.
  6. Add `writeActive` state variable.

### Wave 2 (depends on Wave 1 rename completing)

- **Task 2.1: Implement async write path in claude_bridge** — Agent B — files: `devcon_server/claude_bridge.py`

  1. Add `_extract_session_uuid(session_id)`: return `Path(session_id).stem`.
  2. Add `_extract_cwd(path)`: read JSONL for first `user` record's `cwd` field, with directory name fallback.
  3. Add `CLAUDE_BINARY` resolution (VSCode extension path, then PATH).
  4. Replace `send_input()` with `async def send_input_async(session_id, text) -> dict`:
     - Build command: `[CLAUDE_BINARY, "--print", "--resume", uuid, "--permission-mode", "bypassPermissions", text]`
     - Use `asyncio.create_subprocess_exec` with `stdout=PIPE, stderr=PIPE, cwd=cwd`
     - `await process.communicate()` with 300s timeout
     - Return `{"success": bool, "returncode": int, "stderr": str}`
  5. Remove old synchronous `send_input()`.

- **Task 2.2: Wire write path in client with signals and concurrency guard** — Agent B — files: `devcon_server/client.py`

  1. Add `_active_writes: dict[str, asyncio.Task]` to `DevconClient.__init__`.
  2. Rewrite `_handle_session_input`:
     - Check concurrent write guard → reject if busy.
     - Send `write_started` signal.
     - Create async task for `send_input_async()`.
     - On completion, send `write_ended` with success/error.
     - Remove from `_active_writes` in `finally` block.
  3. Tail remains running — no suppression needed.

- **Task 2.3: Update CLAUDE.md message types** — Agent A — files: `CLAUDE.md`

  1. Add `write_started` and `write_ended` to message types table.

### Wave 3 (testing, depends on Wave 2)

- **Task 3.1: Integration test script** — Agent D — files: `scripts/test_write_live.py`

  1. Create script: list sessions, pick most recent, send test message via `send_input_async()`, monitor via `tail_session()`.

## Acceptance Criteria

- [ ] Mobile user can type a message and receive a Claude agent response
- [ ] Response appears in mobile app via existing `tail_session` → `session_output` path
- [ ] User message is persisted in session JSONL (CLI writes it via `--resume`)
- [ ] `write_started` signal disables mobile input + shows spinner
- [ ] `write_ended` signal re-enables input (with success/error status)
- [ ] Concurrent writes to same session are rejected
- [ ] Errors (CLI not found, timeout, session not found) surface to mobile
- [ ] `interceptor/` renamed to `devcon_server/` throughout
- [ ] Hub WS endpoint renamed from `/ws/interceptor/` to `/ws/devcon/`
- [ ] Mobile `session_input` sends `text` field (not `content`)
- [ ] Mobile properly renders `session_output` with `messages` array and content blocks

## Open Questions

1. **De-duplication strategy**: During a write, `tail_session()` keeps running and will detect the assistant response blocks in the JSONL. The CLI stdout is NOT streamed to mobile (we use `--print` which just prints and exits). So there is NO duplication — tail is the sole content delivery mechanism. This is resolved by design.

2. **Concurrent VSCode + mobile access**: MVP limitation — document as "do not use both simultaneously." Primary use case is mobile-when-away-from-desk.

3. **CLI timeout duration**: 300s proposed. Agent responses with many tool uses could exceed this. Should this be configurable?

4. **`--permission-mode bypassPermissions` safety**: Bypasses all permission checks. Acceptable for MVP but should be documented.

5. **`--resume` session cwd**: Should the CLI be run from the project cwd (extracted from JSONL) or from the repo root? The CLI needs the correct cwd to find the session file.
