"""Module-name guard: load_module must never write outside modules/."""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from heroku_mcp.server import load_module


def _run(coro):
    return asyncio.run(coro)


class ModuleNameGuardTest(unittest.TestCase):
    def test_load_module_rejects_traversal(self):
        result = _run(load_module("../.ssh/authorized_keys", code="x"))
        self.assertIn("invalid module name", result)

    def test_load_module_rejects_slash(self):
        result = _run(load_module("sub/dir/mod", code="x"))
        self.assertIn("invalid module name", result)

    def test_load_module_rejects_dotdot(self):
        result = _run(load_module("..", code="x"))
        self.assertIn("invalid module name", result)

    def test_load_module_accepts_normal_name(self):
        # A valid name passes the guard and proceeds to the file check.
        with mock.patch("heroku_mcp.server.settings") as s:
            s.modules_path = Path("/nonexistent-modules")
            with self.assertRaises(FileNotFoundError):
                _run(load_module("demo", code="print(1)"))


if __name__ == "__main__":
    unittest.main()
