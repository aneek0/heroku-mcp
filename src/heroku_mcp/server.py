"""MCP server for managing the Heroku Telegram userbot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from .config import settings
from .module_store import start_server as start_http, stop_server as stop_http, get_bound_port
from .telegram import ensure_watcher, get_client, send_command, shutdown

log = logging.getLogger("heroku_mcp")

BLOCKED_COMMANDS = frozenset({
    ".delsgroup", ".inlinesec", ".owneradd", ".ownerrm",
    ".sgroupadd", ".sgroupdel", ".tsecclr", ".tsecrm",
    ".remove_core_protection", ".addacc", ".weburl",
    ".ch_bot_token", ".ch_heroku_bot", ".clearmodules",
    ".cleardb", ".clearmodule",
})
_BLOCKED_PREFIXES = [f"{cmd} " for cmd in BLOCKED_COMMANDS]


@asynccontextmanager
async def _lifespan_ctx(app: FastMCP) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle."""
    await ensure_watcher()
    await start_http()
    log.info("Heroku MCP server ready on :%d", settings.server_port)
    yield
    await stop_http()
    await shutdown()


mcp = FastMCP(
    "heroku-mcp",
    instructions="MCP server for managing the Heroku Telegram userbot",
    lifespan=_lifespan_ctx,
    port=settings.server_port,
)


_rx_clean = re.compile(r"[`▪️▸▹◆►▶★]")
_rx_spaces = re.compile(r"\s{2,}")
_rx_line = re.compile(r"^(\w+):\s*\((.+)\)\s*$")


def _parse_help(text: str) -> list[dict]:
    modules = []
    for line in text.splitlines():
        line = _rx_spaces.sub(" ", _rx_clean.sub("", line)).strip()
        if not line:
            continue
        m = _rx_line.match(line)
        if m:
            modules.append({"name": m.group(1), "commands": m.group(2).strip()})
    return modules


async def _execute(cmd: str, wait: float = 5.0) -> str:
    return await send_command(cmd, wait=wait)


@mcp.tool()
async def load_module(name: str, code: str) -> str:
    """Save a Python module, serve it via HTTP, and send .dlm to Heroku. Also works to update an already-loaded module without unloading it first."""
    path = settings.modules_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    log.info("Saved module %s (%d bytes)", name, len(code))

    await start_http()

    port = get_bound_port() or (settings.server_port + 1)
    url = f"http://127.0.0.1:{port}/{name}.py"
    response = await _execute(f".dlm {url}", wait=8.0)
    return response


@mcp.tool()
async def unload_module(name: str) -> str:
    """Unload a module from the running Heroku userbot."""
    response = await _execute(f".ulm {name}")
    return response


@mcp.tool()
async def list_modules() -> str:
    """List currently loaded modules on Heroku."""
    raw = await send_command(".help", wait=6.0)
    modules = _parse_help(raw)
    return json.dumps(modules, ensure_ascii=False, indent=2)


@mcp.tool()
async def evaluate(expr: str) -> str:
    """Evaluate a Python expression on the running Heroku userbot."""
    response = await _execute(f".e {expr}")
    return response


@mcp.tool()
async def send_command_tool(cmd: str, wait: float = 5.0) -> str:
    """Send a raw command to the Heroku userbot."""
    # Security: block destructive commands
    stripped = cmd.strip()
    for prefix, pfx_space in zip(BLOCKED_COMMANDS, _BLOCKED_PREFIXES):
        if stripped == prefix or stripped.startswith(pfx_space):
            return f"🚫 BLOCKED: command '{prefix}' is not allowed via MCP"

    response = await send_command(cmd, wait=wait)
    return response


@mcp.tool()
async def get_history(limit: int = 20) -> str:
    """Get recent messages from Saved Messages for diagnostics."""
    from .telegram import _resolve_entity
    client = await get_client()
    entity = await _resolve_entity()
    target = entity or await client.get_me()
    messages = await client.get_messages(target, limit=limit)
    lines = []
    for msg in reversed(messages):
        if msg.text:
            date_str = msg.date.isoformat() if msg.date else "?"
            lines.append(f"--- #{msg.id} ({date_str}) ---")
            lines.append(msg.text[:500])
    return "\n".join(lines)


@mcp.tool()
async def get_history_json(limit: int = 20) -> str:
    """Get recent messages from Saved Messages as JSON."""
    from .telegram import _resolve_entity
    client = await get_client()
    entity = await _resolve_entity()
    target = entity or await client.get_me()
    messages = await client.get_messages(target, limit=limit)
    result = []
    for msg in reversed(messages):
        if msg.text:
            result.append({
                "id": msg.id,
                "text": msg.text[:500],
                "date": msg.date.isoformat() if msg.date else None,
            })
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    import uvicorn

    from starlette.responses import PlainTextResponse
    from starlette.types import ASGIApp, Receive, Scope, Send

    class RejectGet:
        def __init__(self, app: ASGIApp):
            self.app = app
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["method"] == "GET":
                response = PlainTextResponse("Method Not Allowed", 405)
                await response(scope, receive, send)
            else:
                await self.app(scope, receive, send)

    starlette_app = mcp.streamable_http_app()
    wrapped = RejectGet(starlette_app)

    config = uvicorn.Config(
        wrapped,
        host="127.0.0.1",
        port=settings.server_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
