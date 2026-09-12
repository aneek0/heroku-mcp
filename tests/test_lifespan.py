"""Lifespan regression tests.

Found by live E2E testing (2026-09-12): an unauthorized Telegram session
raised RuntimeError inside ensure_watcher(), which ran in the FastMCP
lifespan. The lifespan crash killed the whole MCP session during
initialize() — the client hung forever and the actionable error message
from get_client() was only visible in server logs.

The lifespan must survive Telegram startup failures so that tool calls
can surface the error to the client instead (the SDK converts exceptions
in tools into isError=True results the client actually sees).
"""

import asyncio
import unittest
from unittest import mock

from heroku_mcp import server


def _run(coro):
    return asyncio.run(coro)


class LifespanTelegramFailureTest(unittest.TestCase):
    def _patch_io(self):
        return (
            mock.patch.object(server, "start_http", mock.AsyncMock()),
            mock.patch.object(server, "stop_http", mock.AsyncMock()),
            mock.patch.object(server, "shutdown", mock.AsyncMock()),
        )

    def test_lifespan_survives_unauthorized_session(self):
        async def unauthorized():
            raise RuntimeError(
                "Telegram session is not authorized. Run "
                "'python generate_session.py --phone +<number>' to log in"
                " (it honors the proxy setting), then retry this tool -"
                " no server restart is needed."
            )

        async def run_case(entered):
            patches = list(self._patch_io()) + [
                mock.patch.object(server, "ensure_watcher", unauthorized),
            ]
            with (
                patches[0], patches[1], patches[2], patches[3]
            ):
                async with server._lifespan_ctx(mock.Mock()):
                    entered["ok"] = True

        entered = {}
        _run(run_case(entered))
        self.assertTrue(entered.get("ok"), "lifespan must not raise when Telegram is unavailable")

    def test_lifespan_survives_network_failure(self):
        async def unreachable():
            raise OSError("Connection to 203.0.113.1:1443 failed")

        async def run_case(entered):
            patches = list(self._patch_io()) + [
                mock.patch.object(server, "ensure_watcher", unreachable),
            ]
            with (
                patches[0], patches[1], patches[2], patches[3]
            ):
                async with server._lifespan_ctx(mock.Mock()):
                    entered["ok"] = True

        entered = {}
        _run(run_case(entered))
        self.assertTrue(entered.get("ok"), "lifespan must not raise on Telegram network errors")

    def test_lifespan_still_runs_module_store_when_telethon_fails(self):
        async def broken():
            raise RuntimeError("boom")

        async def run_case(captured):
            with mock.patch.object(server, "ensure_watcher", broken):
                with mock.patch.object(server, "start_http", mock.AsyncMock()) as start_mock:
                    captured["start"] = start_mock
                    async with server._lifespan_ctx(mock.Mock()):
                        pass

        captured = {}
        _run(run_case(captured))
        self.assertTrue(
            captured["start"].await_count >= 1,
            "module store must still start even if Telegram is down",
        )


if __name__ == "__main__":
    unittest.main()
