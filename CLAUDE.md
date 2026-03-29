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

### 2. Interceptor Server (`interceptor/`)
- Runs inside each devcontainer
- Reads from and writes to the VSCode Claude Code extension
- Read path is straightforward; write path needs state consistency
  (devcontainer state must match mobile writes)
- Candidate approach: CLI puppeting, since the VSCode Claude ext uses the CLI
- Requires further testing for write consistency

### 3. Hub Server (`hub_server/`)
- Runs on the host machine
- Interceptors connect to the hub
- Hub exposes access to the mobile app via TailScale
- Manages connections between interceptors and mobile clients

## MVP Scope

- 1 devcontainer, 1 hub, 1 mobile app
- Successfully read and write a Claude Code session from mobile
- No additional coding tools beyond session interaction

## Tech Stack

- **Mobile**: PWA (single-file HTML/CSS/JS), dark theme matching VSCode
- **Hub Server**: Python, FastAPI, WebSockets, uvicorn — port 8420
- **Interceptor**: Python, websockets, asyncio — CLI puppeting via `claude` CLI
- **Protocol**: WebSocket JSON messages with `type` field
- **Networking**: TailScale for mobile ↔ hub connectivity

## Message Types

| Type | Direction | Purpose |
|------|-----------|---------|
| `session_list` | mobile→hub→interceptor→hub→mobile | List available sessions |
| `session_select` | mobile→hub→interceptor | Start watching a session |
| `session_output` | interceptor→hub→mobile | Session content updates |
| `session_input` | mobile→hub→interceptor | Send input to a session |

## Package Management

Uses **uv** with a single `pyproject.toml` at the repo root. Both hub_server and
interceptor share the same dependency set (no per-component requirements files).

```bash
uv sync        # install/update all deps
```

## Commands

```bash
# Hub server (run on host, port 8420)
uv run python -m hub_server

# Interceptor (run inside devcontainer)
uv run python -m interceptor --hub-host <host-ip> --hub-port 8420

# Mobile dev server (port 8421)
uv run python mobile/serve.py
```
