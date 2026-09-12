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

^- `src/heroku_mcp/server.py` — MCP tool definitions (`main()`)
^- `src/heroku_mcp/config.py` — settings singleton (`from .config import settings`)
^- `src/heroku_mcp/telegram.py` — Telegram client, command sending
^- `src/heroku_mcp/module_store.py` — HTTP server for `.py` distribution

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
- `proxy` — optional proxy: `tg://proxy?server=…&port=…&secret=…` (MTProto, dd/ee/plain secrets), `socks5://[user:pass@]host:port`, `socks4://`, `http://`, `tg://socks?…`; parsed in `proxy.py`, invalid spec fails fast (no silent direct fallback); needs `python-socks[asyncio]` for SOCKS/HTTP
- Blocked commands: `BLOCKED_COMMANDS` frozenset in `server.py`
- Session lock: fcntl-based, handled in `telegram.py`; `generate_session.py` participates in the same protocol — it refuses to start while another process (e.g. a running server) holds the lock
- `get_client()` uses `connect()` + `is_user_authorized()` (NOT interactive `start()` — the phone prompt kills MCP sessions); retries connect 3×; unauthorized session → clear error pointing to `generate_session.py`; after authorizing, retry the tool — no server restart needed (lifespan survives Telegram failures, tools re-create the client)
- Module loading: write `.py`, serve via aiohttp, send `.dlm <url>` to userbot
- Session generation: `generate_session.py` (honors proxy, respects the session lock)

## Tests

Offline unit tests (no network):

    .venv/bin/python -m unittest discover -s tests -v

Covers: proxy parser (MTProto dd/ee/plain, socks5/4, http, tg://socks, invalid specs), log descriptions never leak secrets/passwords, TelegramClient construction with each proxy kind, yaml/env precedence for the proxy key, lifespan survives Telegram startup failures (unauthorized/network errors, module store still starts), generate_session.py flock acquire/release + refusal with actionable guidance when the lock is held.
