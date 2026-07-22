"""AIOHTTP server that serves module .py files for .dlm loading."""

from __future__ import annotations

import logging
from aiohttp import web

from .config import settings

log = logging.getLogger(__name__)

_runner: web.AppRunner | None = None
_bound_port: int | None = None


async def _handle_module(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = settings.modules_path / f"{name}.py"
    if not path.is_file():
        raise web.HTTPNotFound(text=f"Module {name} not found")
    return web.Response(
        text=path.read_text(encoding="utf-8"),
        content_type="text/x-python",
    )


async def start_server() -> None:
    global _runner, _bound_port
    if _runner is not None:
        return

    _app = web.Application()
    _app.router.add_get("/{name}.py", _handle_module)

    _runner = web.AppRunner(_app)
    await _runner.setup()

    # Try configured port +1, then scan upward
    for port in range(settings.server_port + 1, settings.server_port + 101):
        try:
            site = web.TCPSite(_runner, "127.0.0.1", port)
            await site.start()
            _bound_port = port
            log.info("Module server listening on :%d", port)
            return
        except OSError:
            continue

    await _runner.cleanup()
    _runner = None
    log.error("Could not bind module server to any port in range %d-%d",
              settings.server_port + 1, settings.server_port + 100)


def get_bound_port() -> int | None:
    """Return the actual port the server is listening on."""
    return _bound_port


async def stop_server() -> None:
    global _runner, _bound_port
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
        _bound_port = None
