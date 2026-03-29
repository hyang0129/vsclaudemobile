# vsclaudemobile

## Purpose

Mobile mirror of the VSCode Claude Code extension experience. Lets you interact
with Claude Code sessions from your phone, connected to devcontainer workspaces.

## Architecture (3 components)

### 1. Mobile App (`mobile/`)
- Mirrors the VSCode Claude Code extension interface on a phone
- Primary use case: remote interaction with devcontainer sessions
- Navigation hierarchy: **Hub → Window (devcontainer) → Session (claude ext session)**
- MVP: single hub, single devcontainer, read + write a claude session

### 2. Devcon Server (`devcon_server/`)
- Runs inside each devcontainer
- Reads from and writes to the VSCode Claude Code extension
- Read path: discovers and tails JSONL session files under `~/.claude/projects/`
- Write path: spawns `claude --print --resume <uuid>` to send messages to sessions
- Sends `write_started`/`write_ended` signals to mobile during writes

### 3. Hub Server (`hub_server/`)
- Runs on the host machine
- Devcon servers connect to the hub
- Hub exposes access to the mobile app via TailScale
- Manages connections between devcon servers and mobile clients

## MVP Scope

- 1 devcontainer, 1 hub, 1 mobile app
- Successfully read and write a Claude Code session from mobile
- No additional coding tools beyond session interaction

## Tech Stack

- **Mobile**: PWA (single-file HTML/CSS/JS), dark theme matching VSCode
- **Hub Server**: Python, FastAPI, WebSockets, uvicorn — port 8420
- **Devcon Server**: Python, websockets, asyncio — CLI invocation via `claude` CLI
- **Protocol**: WebSocket JSON messages with `type` field
- **Networking**: TailScale for mobile ↔ hub connectivity

## Message Types

| Type | Direction | Purpose |
|------|-----------|---------|
| `session_list` | mobile→hub→devcon→hub→mobile | List available sessions |
| `session_select` | mobile→hub→devcon | Start watching a session |
| `session_output` | devcon→hub→mobile | Session content updates |
| `session_input` | mobile→hub→devcon | Send input to a session |
| `write_started` | devcon→hub→mobile | Write operation began (disable input) |
| `write_ended` | devcon→hub→mobile | Write operation finished (re-enable input) |

## Package Management

Uses **uv** with a single `pyproject.toml` at the repo root. Both hub_server and
devcon_server share the same dependency set (no per-component requirements files).

```bash
uv sync        # install/update all deps
```

## Logging

Use **loguru** (`from loguru import logger`) for all logging — never stdlib `logging`.
**Overlog rather than underlog.** Every function entry, exit, branch, and data transformation
should have a log call. Use TRACE for per-record/per-line detail, DEBUG for function-level
flow, INFO for operations and results, WARNING/ERROR for problems.

Log files are written to `/tmp/vsclaudemobile/` at TRACE level regardless of the console
log level. This makes troubleshooting possible even when running at INFO on the console.

When adding new code: if in doubt, add a log line. Logs are cheap; debugging without them is not.

## Commands

```bash
# Hub server (run on host, port 8420)
uv run python -m hub_server

# Devcon server (run inside devcontainer)
uv run python -m devcon_server --hub-host <host-ip> --hub-port 8420

# Mobile dev server (port 8421)
uv run python mobile/serve.py
```
