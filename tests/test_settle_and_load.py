"""send_command settles on the final answer, not the first edit.

Live behaviour (2026-09-16): some userbot commands edit the sent message
several times — ``.dlm`` prints "installing..." first and the load result a
second later. ``send_command`` used to return on the first edit, so callers
saw the intermediate stage. Now edits are tracked until ``quiet`` seconds
pass without a new one, and the last text wins.

The staged scenario below is exercised through the *poll* path (no live
events), which is the deterministic offline path: each poll returns the next
stage, and settling must only happen after the final stage has been quiet
for ``quiet`` seconds.
"""

import asyncio
import unittest
from unittest import mock

from heroku_mcp import telegram
from heroku_mcp.server import load_module, _execute


def _run(coro):
    return asyncio.run(coro)


class _FakeMsg:
    def __init__(self, id=1, text=""):
        self.id = id
        self.text = text


class _FakeClient:
    """Minimal Telethon stand-in: poll delivers a scripted edit sequence."""

    def __init__(self, stages, gaps):
        # stages: texts returned by successive get_messages polls (first poll
        # happens ~0.25 s after send). gaps: seconds of sleep before each
        # poll returns the next stage (0 for the first).
        self._stages = list(stages)
        self._gaps = list(gaps)
        self.handlers = []

    def on(self, *args, **kwargs):
        def deco(fn):
            self.handlers.append(fn)
            return fn
        return deco

    def remove_event_handler(self, fn, *args):
        if fn in self.handlers:
            self.handlers.remove(fn)

    def add_event_handler(self, fn, *args):
        self.handlers.append(fn)

    async def get_me(self):
        return _FakeMsg(id=42, text="me")

    async def send_message(self, target, text, **kwargs):
        return _FakeMsg(id=7, text=text)

    async def get_messages(self, entity, ids=None, limit=None):
        if self._stages:
            gap = self._gaps.pop(0) if self._gaps else 0
            if gap:
                await asyncio.sleep(gap)
            return _FakeMsg(id=7, text=self._stages.pop(0))
        return _FakeMsg(id=7, text=None)


def _patch_telegram(client):
    async def fake_get_client():
        return client

    async def fake_resolve_entity():
        return None  # forces target = me (id=42)

    return (
        mock.patch.object(telegram, "get_client", fake_get_client),
        mock.patch.object(telegram, "_resolve_entity", fake_resolve_entity),
        mock.patch.object(telegram, "settings", telegram.settings),
    )


class SettleTest(unittest.TestCase):
    def test_returns_last_stage_not_first(self):
        # .dlm shape: "installing..." now, final result 1 s later.
        client = _FakeClient(
            stages=["cmd", "🕔 Устанавливаю модуль…", "🪐 Модуль demo загружен", None],
            gaps=[0, 0, 1.0],
        )
        p1, p2, _ = _patch_telegram(client)
        with p1, p2:
            result = _run(
                telegram.send_command(".dlm http://x/demo.py", wait=15.0, quiet=2.0)
            )
        self.assertEqual(result, "🪐 Модуль demo загружен")

    def test_quiet_cut_keeps_last_text(self):
        # Polls keep repeating the same text: the settle cut must return it,
        # not wait for the full ``wait`` ceiling.
        client = _FakeClient(stages=["cmd", "done", None], gaps=[0, 0])
        p1, p2, _ = _patch_telegram(client)
        with p1, p2:
            result = _run(telegram.send_command(".help", wait=20.0, quiet=2.0))
        self.assertEqual(result, "done")

    def test_no_response_returns_empty(self):
        # No edits at all: falls through to the wait ceiling with "".
        client = _FakeClient(stages=[], gaps=[])
        p1, p2, _ = _patch_telegram(client)
        with p1, p2:
            result = _run(telegram.send_command(".stuck", wait=0.6, quiet=0.5))
        self.assertEqual(result, "")

    def test_timeout_returns_empty_string(self):
        async def fake_get_client():
            return _FakeClient(stages=[], gaps=[])

        with (
            mock.patch.object(telegram, "get_client", fake_get_client),
            mock.patch.object(telegram, "_resolve_entity", mock.AsyncMock(return_value=None)),
        ):
            result = _run(telegram.send_command(".x", wait=0.6, quiet=0.5))
        self.assertEqual(result, "")

    def test_send_command_final_is_alias(self):
        client = _FakeClient(stages=["cmd", "final"], gaps=[0, 0])
        p1, p2, _ = _patch_telegram(client)
        with p1, p2:
            result = _run(
                telegram.send_command_final(".restart -f", wait=10.0, quiet=1.0)
            )
        self.assertEqual(result, "final")


class LoadModuleTest(unittest.TestCase):
    def _settings_tmp(self, tmp):
        from heroku_mcp.config import settings
        return mock.patch.object(
            type(settings), "modules_path",
            new=mock.PropertyMock(return_value=tmp),
        )

    def _patched_settings(self, real_settings):
        # pydantic BaseSettings rejects attribute assignment for methods;
        # patch the class method instead and keep the real instance in place.
        return mock.patch.object(
            type(real_settings), "module_url",
            lambda self, name, port: f"http://store/{name}.py",
        )

    def test_reload_existing_file_without_code(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.py").write_text("NEWCODE\n", encoding="utf-8")
            client = _FakeClient(stages=[".dlm …", "🪐 Модуль demo загружен", None], gaps=[0, 0])

            async def fake_start_http():
                pass

            with (
                self._settings_tmp(tmp_path),
                mock.patch("heroku_mcp.server.start_http", fake_start_http),
                mock.patch("heroku_mcp.server.get_bound_port", return_value=6768),
                self._patched_settings(telegram.settings),
                mock.patch("heroku_mcp.server.send_command",
                           mock.AsyncMock(return_value="🪐 Модуль demo загружен")) as sc,
            ):
                result = _run(load_module("demo"))

            self.assertEqual(result, "🪐 Модуль demo загружен")
            sent = sc.call_args.args[0]
            self.assertTrue(sent.startswith(".dlm "))
            # Existing file untouched by the reload path.
            self.assertEqual((tmp_path / "demo.py").read_text(), "NEWCODE\n")

    def test_missing_file_without_code_is_error(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self._settings_tmp(tmp_path):
                result = _run(load_module("ghost"))
            self.assertIn("❌", result)
            self.assertIn("ghost", result)

    def test_with_code_writes_file_and_sends_dlm(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            async def fake_start_http():
                pass

            with (
                self._settings_tmp(tmp_path),
                mock.patch("heroku_mcp.server.start_http", fake_start_http),
                mock.patch("heroku_mcp.server.get_bound_port", return_value=6768),
                self._patched_settings(telegram.settings),
                mock.patch("heroku_mcp.server.send_command",
                           mock.AsyncMock(return_value="ok")) as sc,
            ):
                result = _run(load_module("fresh", code="print(1)\n"))
            self.assertEqual(result, "ok")
            self.assertEqual((tmp_path / "fresh.py").read_text(), "print(1)\n")
            self.assertTrue(sc.call_args.args[0].startswith(".dlm "))


class ExecuteTest(unittest.TestCase):
    def test_execute_passes_wait_through(self):
        with mock.patch("heroku_mcp.server.send_command",
                        mock.AsyncMock(return_value="r")) as sc:
            result = _run(_execute(".e 1+1", wait=12.0))
        self.assertEqual(result, "r")
        self.assertEqual(sc.call_args.kwargs, {"wait": 12.0})


if __name__ == "__main__":
    unittest.main()
