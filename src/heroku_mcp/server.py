"""MCP server for managing the Heroku Telegram userbot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from .config import settings
from .module_store import start_server as start_http, stop_server as stop_http, get_bound_port
from .telegram import (
    ensure_watcher,
    get_client,
    send_command,
    set_target_chat,
    shutdown,
    _call_with_recovery,
    _resolve_entity,
)

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
    try:
        await ensure_watcher()
    except Exception as e:
        # Telegram problems (unauthorized session, invalid proxy, unreachable
        # network) must not crash the MCP session at initialize: then the
        # client would just hang with no actionable message. Tools call
        # get_client() themselves and the SDK converts the exception into an
        # isError tool result the client can actually see.
        log.warning("Telegram unavailable at startup; tools will report it: %s", e)
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
async def restart_userbot(verify: bool = True) -> str:
    """Force-restart the Heroku userbot via `.restart -f` and report the final restart status."""
    from .telegram import send_command_final
    # Restart edits can be spread over the whole boot: use boot_wait both as
    # the settle window between edits and to scale the overall deadline.
    boot_wait = settings.restart_boot_wait
    final = await send_command_final(".restart -f", wait=boot_wait * 2 + 30, quiet=float(boot_wait))
    if not final:
        return "(no response — userbot did not report restart)"
    if verify:
        boot_wait = settings.restart_boot_wait
        log.info("Waiting %ds for userbot to boot before verification", boot_wait)
        await asyncio.sleep(boot_wait)
        for attempt in range(3):
            check = await _execute(".help", wait=6.0)
            if check != "(no response)":
                return f"{final}\n✅ userbot is back (attempt {attempt + 1})"
            await asyncio.sleep(10)
        return f"{final}\n⚠️ no confirmation after restart — check manually"
    return final


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


def _topic_filter(msg) -> bool:
    """True if msg belongs to the configured topic (or no topic set)."""
    topic = settings.her_topic_id
    if not topic:
        return True
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return False
    # Messages inside a topic reply to the topic's root message id.
    if getattr(reply, "forum_topic", False):
        return True
    return getattr(reply, "reply_to_msg_id", None) == topic or \
        getattr(reply, "reply_to_top_id", None) == topic


@mcp.tool()
async def get_history(limit: int = 20) -> str:
    """Get recent messages from the configured chat/topic for diagnostics."""
    client = await get_client()
    entity = await _resolve_entity()
    target = entity or await client.get_me()
    messages = await _call_with_recovery(
        lambda: client.get_messages(target, limit=max(limit * 3, limit))
    )
    lines = []
    for msg in reversed(messages):
        if not msg or not msg.text or not _topic_filter(msg):
            continue
        date_str = msg.date.isoformat() if msg.date else "?"
        lines.append(f"--- #{msg.id} ({date_str}) ---")
        lines.append(msg.text[:500])
        if len([l for l in lines if l.startswith("--- #")]) >= limit:
            break
    return "\n".join(lines)


@mcp.tool()
async def get_history_json(limit: int = 20) -> str:
    """Get recent messages from the configured chat/topic as JSON."""
    client = await get_client()
    entity = await _resolve_entity()
    target = entity or await client.get_me()
    messages = await _call_with_recovery(
        lambda: client.get_messages(target, limit=max(limit * 3, limit))
    )
    result = []
    for msg in reversed(messages):
        if not msg or not msg.text or not _topic_filter(msg):
            continue
        result.append({
            "id": msg.id,
            "text": msg.text[:500],
            "date": msg.date.isoformat() if msg.date else None,
        })
        if len(result) >= limit:
            break
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def switch_chat(chat: str, topic: int = 0) -> str:
    """Switch the chat (and optional forum topic) all commands are sent to.

    Accepts:
    - "me" (Saved Messages), chat id, @username
    - topic link: "https://t.me/c/3537976236/7" or "https://t.me/<username>/<topic>" —
      the topic is extracted from the link automatically
    - explicit topic: pass chat id/username + topic number
    """
    return await set_target_chat(chat, topic)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if os.environ.get("HEROKU_MCP_STDIO", "").strip().lower() in ("1", "true", "yes"):
        # stdio transport for MCP clients that don't support streamable HTTP
        # (e.g. jcode). Logs must NOT go to stdout — that is the protocol channel.
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
        mcp.run(transport="stdio")
        return

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
