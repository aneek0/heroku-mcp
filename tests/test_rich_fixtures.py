"""Rich-message rendering against real userbot replies (captured 2026-10-21).

Since 2026-10 the Heroku userbot answers through an inline bot with a rich
message: ``Message.text`` is empty and the content lives in
``Message.rich_message``. The fixtures in ``tests/fixtures/`` are raw
``RichMessage`` JSON captured from the live Logs topic:

- ``fixture_ping.json``  — `.ping` answer (TextConcat with TextBold/TextFixed)
- ``fixture_help.json``  — `.help` listing (PageBlockDetails with commands)
- ``fixture_plain.json`` — a plain (non-rich) message, for the passthrough path
"""

import asyncio
import json
import inspect
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _build(d):
    """Rebuild a Telethon TLObject from its to_dict() JSON."""
    if isinstance(d, list):
        return [_build(x) for x in d]
    if not isinstance(d, dict) or "_" not in d:
        return d
    from telethon.tl import types as T

    cls = getattr(T, d["_"])
    params = inspect.signature(cls.__init__).parameters
    kw = {k: _build(v) for k, v in d.items() if k != "_" and k in params}
    return cls(**kw)


def _load(name):
    data = json.loads((FIXTURES / name).read_text())
    rich = _build(data["rich_message"]) if data.get("rich_message") else None
    return data, rich


class RichFixtureTest(unittest.TestCase):
    def test_ping_answer_renders(self):
        from heroku_mcp.rich import rich_to_text

        data, rich = _load("fixture_ping.json")
        self.assertEqual(data["text"], "")
        text = rich_to_text(rich)
        self.assertIn("𝙿𝚒𝚗𝚐:", text)
        self.assertIn("𝚄𝚙𝚝𝚒𝚖𝚎:", text)
        self.assertRegex(text, r"66\.833")

    def test_help_answer_renders_module_lines(self):
        from heroku_mcp.rich import rich_to_text
        from heroku_mcp.server import _parse_help

        _, rich = _load("fixture_help.json")
        text = rich_to_text(rich)
        modules = _parse_help(text)
        by_name = {m["name"]: m["commands"] for m in modules}
        self.assertIn("Tester", by_name)
        self.assertIn("ping", by_name["Tester"])
        self.assertIn("Loader", by_name)
        self.assertIn("dlmall", by_name["Loader"])
        self.assertGreater(len(modules), 30)

    def test_plain_message_passthrough(self):
        from heroku_mcp.rich import rich_to_text

        data, rich = _load("fixture_plain.json")
        self.assertIsNone(rich)
        self.assertEqual(rich_to_text(rich), "")

    def test_message_text_prefers_plain_text(self):
        from heroku_mcp.rich import message_text

        class _Msg:
            text = "plain answer"
            rich_message = None

        self.assertEqual(message_text(_Msg()), "plain answer")

    def test_message_text_falls_back_to_rich(self):
        from heroku_mcp.rich import message_text

        class _Rich:
            blocks = []

        class _Msg:
            text = ""
            rich_message = _Rich()

        self.assertEqual(message_text(_Msg()), "")


if __name__ == "__main__":
    unittest.main()
