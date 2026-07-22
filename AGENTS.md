# AGENTS.md — heroku-mcp

## Project

MCP server for managing a Heroku Telegram userbot via MCP protocol.

## Stack

- Python 3.11+, hatchling build
- `telethon` — Telegram client
- `mcp[server]` — MCP protocol server (Streamable HTTP via uvicorn)
- `aiohttp` — HTTP server for module distribution
- `pydantic-settings` — config (yaml + env)

## Entry points

- `heroku_mcp/server.py` — MCP tool definitions (`main()`)
- `heroku_mcp/config.py` — settings singleton (`from .config import settings`)
- `heroku_mcp/telegram.py` — Telegram client, command sending
- `heroku_mcp/module_store.py` — HTTP server for `.py` distribution

## Architecture

MCP client → JSON-RPC over HTTP (port 6767) → `server.py` tool handler →
`send_command()` in `telegram.py` → Telethon sends message to `her_chat_id` →
userbot edits the message with result → response caught by `MessageEdited`
event handler or poll fallback (`get_messages`).

## Conventions

- Use `from __future__ import annotations` in all modules
- Log via module-level `log = logging.getLogger(__name__)`
- `_execute(cmd, wait=5.0)` for userbot commands
- Absolute paths resolved in `config.py` via `_resolve_project_path`

## Key details

- Config: `config.yaml` → `HerokuMcpSettings` singleton
- `her_chat_id` — target chat (default `"me"` = Saved Messages, or group ID)
- `her_topic_id` — forum topic ID for group chats (0 = disabled)
- Blocked commands: `BLOCKED_COMMANDS` frozenset in `server.py`
- Session lock: fcntl-based, handled in `telegram.py`
- Module loading: write `.py`, serve via aiohttp, send `.dlm <url>` to userbot
- Session generation: `generate_session.py`

## Tests

No test framework configured. Run manually: `python -m heroku_mcp.server`
