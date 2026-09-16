"""MCP server for managing the Heroku Telegram userbot."""

from __future__ import annotations

import asyncio
import hmac
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
    settings.warn_if_module_store_public()
    log.info("Heroku MCP server ready on %s:%d", settings.server_host, settings.server_port)
    yield
    await stop_http()
    await shutdown()


mcp = FastMCP(
    "heroku-mcp",
    instructions="MCP server for managing the Heroku Telegram userbot",
    lifespan=_lifespan_ctx,
    host=settings.server_host or "127.0.0.1",
    port=settings.server_port,
    # FastMCP only guesses loopback names, which rejects every remote client
    # with 421. Build the list from the settings instead.
    transport_security=settings.transport_security(),
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


async def _execute(cmd: str, wait: float = 10.0) -> str:
    """Run a userbot command and return its final text.

    ``send_command`` settles on the last edit: typical latency is
    (command runtime) + ~2 s of quiet, ``wait`` is only the emergency
    ceiling for a hung command.
    """
    return await send_command(cmd, wait=wait)


# MCP session ids are bearer-equivalent (each carries a live Telegram-backed
# session), so health probes are the only paths exempt from the token.
OPEN_PATHS = frozenset({"/healthz", "/health"})


def _supplied_token(headers: dict[bytes, bytes]) -> str:
    """Token as `Authorization: Bearer <t>` or `X-Api-Key: <t>`."""
    for name in (b"authorization", b"x-api-key"):
        for key, value in headers.items():
            if key != name:
                continue
            raw = value.decode("latin-1").strip()
            if raw.lower().startswith("bearer "):
                raw = raw[7:].strip()
            if raw:
                return raw
    return ""


def _authorized(headers: dict[bytes, bytes]) -> bool:
    if not settings.auth_token:
        return True
    supplied = _supplied_token(headers)
    return bool(supplied) and hmac.compare_digest(supplied, settings.auth_token)


class Guard:
    """ASGI wrapper: 405 for GET (except health), 401 for a bad token.

    The token is checked at the transport edge rather than per tool, so a
    rejected client never reaches the Telegram layer at all.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        from starlette.responses import PlainTextResponse

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope["method"] == "GET":
            if path in OPEN_PATHS:
                await PlainTextResponse("ok", 200)(scope, receive, send)
            else:
                await PlainTextResponse("Method Not Allowed", 405)(scope, receive, send)
            return
        if not _authorized(dict(scope.get("headers") or {})):
            await PlainTextResponse("Unauthorized", 401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@mcp.tool()
async def load_module(name: str, code: str | None = None) -> str:
    """Load (or update) a module on Heroku by serving it over HTTP and sending .dlm.

    Module files live in the ``modules/`` folder on the MCP-server host; the
    local module store serves them and the userbot downloads from that URL.

    With ``code``: saves the text to ``modules/<name>.py`` (overwrites) and
    loads it. Without ``code``: re-sends ``.dlm`` for the existing file, so
    the module may be edited in place first. The returned text is the final
    userbot answer (loaded / failed + traceback), not the intermediate
    "installing..." stage.
    """
    path = settings.modules_path / f"{name}.py"
    if code is None:
        if not path.is_file():
            return (
                f"❌ module '{name}' not found in {settings.modules_path} — "
                "pass code= to create it, or edit the existing file first"
            )
        log.info("Reloading existing module %s from %s", name, path)
    else:
        path.write_text(code, encoding="utf-8")
        log.info("Saved module %s (%d bytes)", name, len(code))

    await start_http()

    port = get_bound_port() or (settings.server_port + 1)
    url = settings.module_url(name, port)
    log.info("Serving module %s to the userbot at %s", name, url)
    # .dlm edits twice ("installing..." then the result); default quiet=2 s
    # returns the final stage. wait=25 is just the hang ceiling.
    return await send_command(f".dlm {url}", wait=25.0)


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
async def send_command_tool(cmd: str, wait: float = 10.0, quiet: float = 2.0) -> str:
    """Send a raw command to the Heroku userbot.

    Returns the final answer: edits are tracked until ``quiet`` seconds pass
    without a new one. ``wait`` is only the overall ceiling.
    """
    # Security: block destructive commands
    stripped = cmd.strip()
    for prefix, pfx_space in zip(BLOCKED_COMMANDS, _BLOCKED_PREFIXES):
        if stripped == prefix or stripped.startswith(pfx_space):
            return f"🚫 BLOCKED: command '{prefix}' is not allowed via MCP"

    response = await send_command(cmd, wait=wait, quiet=quiet)
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


def build_http_app():
    """ASGI app for the streamable-HTTP transport, with our startup intact.

    FastMCP.streamable_http_app() hands Starlette its own lifespan
    (`session_manager.run()`) and thereby drops the `lifespan=` passed to
    FastMCP(...). That makes the two transports behave differently: stdio
    pre-syncs the channel and serves the module store, HTTP silently did
    neither. Chain the two so the FastMCP lifespan runs in both modes.
    """
    app = mcp.streamable_http_app()
    session_manager_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app):
        async with _lifespan_ctx(mcp):
            async with session_manager_lifespan(scope_app):
                yield

    app.router.lifespan_context = lifespan
    return app


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

    # Fail fast before binding a port: an exposed MCP endpoint without a token
    # is an exposed userbot.
    settings.validate_http_security()

    import uvicorn

    starlette_app = build_http_app()
    wrapped = Guard(starlette_app)

    config = uvicorn.Config(
        wrapped,
        host=settings.server_host or "127.0.0.1",
        port=settings.server_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
