# Plan: Phase 1: Interceptor server - read-only Claude CLI session messages (#1)

## Summary

The interceptor's `claude_bridge.py` already has the structural skeleton for session discovery, JSONL reading, and file tailing, but it passes raw JSONL dicts straight through without filtering or structuring them. This issue adds message parsing that extracts only `user` and `assistant` lines, structures their content blocks (text, thinking, tool_use, tool_result), and surfaces `ai-title` session titles — so the mobile app receives clean, typed data rather than raw JSONL noise.

No new files are needed; all changes land in the single file that owns the parsing logic.

## Affected Files

| File | Change type | Owned by |
|---|---|---|
| `interceptor/claude_bridge.py` | modify | Coder A |

## File Ownership Table

| Agent | Files |
|---|---|
| Coder A | `interceptor/claude_bridge.py` |

## Task List

### Wave 1 (single task, no parallel work needed)

- **Task 1.1: Structured message extraction** — Coder A — files: `interceptor/claude_bridge.py`

  1. Add a private helper `_extract_title(lines: list[dict]) -> str | None` that scans for the first `{"type": "ai-title"}` record and returns its `aiTitle` field.

  2. Add a private helper `_extract_content(content_blocks: list[dict]) -> list[dict]` that normalises the content block list:
     - `{"type": "text", "text": "..."}` → keep as-is
     - `{"type": "thinking", "thinking": "..."}` → keep as-is
     - `{"type": "tool_use", "id": ..., "name": ..., "input": ...}` → keep as-is
     - `{"type": "tool_result", "tool_use_id": ..., "content": ...}` → keep as-is
     - Any other/unknown block type → drop (log at DEBUG level)

  3. Add a private helper `_build_message(raw: dict) -> dict | None` that:
     - Returns `None` for any `type` that is not `"user"` or `"assistant"`.
     - Returns a structured dict:
       ```
       {
         "role": "user" | "assistant",
         "uuid": str,
         "timestamp": str,
         "session_id": str,          # from raw["sessionId"] if present
         "content": [<normalised blocks>],
         "model": str | None,        # assistant only, from raw["message"]["model"]
         "stop_reason": str | None,  # assistant only
       }
       ```
     - For `user` messages whose `content` is a list containing `tool_result` blocks (i.e. `toolUseResult` messages), these are still extracted normally — content blocks carry the type.

  4. Update `_parse_jsonl(path)` to:
     - Collect all raw lines as before.
     - Also scan for the title while iterating (single pass).
     - Return `(messages: list[dict], title: str | None)` — a 2-tuple. **Breaking change to internal API only** (callers are `list_sessions` and `read_session`, both in the same file).

     *Alternatively*: keep `_parse_jsonl` returning raw lines and add a separate `_filter_messages(lines)` function. Either approach is fine; keep it simple.

  5. Update `list_sessions()` to call `_parse_jsonl` (or a new `_read_title` helper) for each session file and include `"title": str | None` in each returned session dict. Note: reading every file on every `session_list` call is acceptable for the MVP given the small number of sessions expected.

  6. Update `read_session(session_id)` to:
     - Parse JSONL via the updated helpers.
     - Return only structured `user`/`assistant` message dicts (not raw lines).
     - Log the count of structured messages, not raw lines.

  7. Update `tail_session()` so new lines emitted via the callback are also filtered through `_build_message` before being passed to the callback — drop `None` results so the caller only sees real messages.

## Acceptance Criteria

- [ ] `list_sessions()` returns a `title` field (string or `None`) for each session entry, populated from `ai-title` lines when present.
- [ ] `read_session(session_id)` returns a list of dicts with keys `role`, `uuid`, `timestamp`, `content`; lines with types other than `user`/`assistant` are absent.
- [ ] Each `content` item in a returned message is one of: `{type:"text", text:...}`, `{type:"thinking", thinking:...}`, `{type:"tool_use", id, name, input}`, `{type:"tool_result", tool_use_id, content}`.
- [ ] `tail_session()` callback receives only structured `user`/`assistant` dicts, never raw `queue-operation`, `file-history-snapshot`, or other metadata lines.
- [ ] `client.py` requires no changes — the structured dicts produced by `claude_bridge` are forwarded as-is via `session_output` messages, which is already correct.
- [ ] Running the interceptor against a real `~/.claude/projects/` directory produces no Python exceptions from the new parsing code.

## Open Questions

- `list_sessions()` currently reads the full JSONL of every session file to find `ai-title`. If session files are very large this could be slow. A future optimisation could scan only the last N lines, but this is out of scope for Phase 1.
- The `_parse_jsonl` signature change (returning a tuple) is an internal-only change, but if tests exist they will need updating. No tests were found in the current codebase, so this is not blocking.
