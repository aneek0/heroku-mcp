"""Session-lock regression tests for generate_session.py.

The MCP server (telegram.py) guards the SQLite session with a flock on
sessions/<name>.session.lock. generate_session.py writes the same session
file; before this fix it ignored the lock entirely, so running the script
while an authorized server held the lock would race it into
'database is locked' SQLite errors.
"""

import contextlib
import fcntl
import importlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "generate_session.py"
PYTHON = sys.executable


def _import_generate_session():
    """Import the script as a module without letting it touch the real session."""
    with contextlib.redirect_stdout(io.StringIO()):
        spec = importlib.util.spec_from_file_location("generate_session_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class GenerateSessionLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name) / "heroku_mcp.session"
        # generate_session writes flock via with_suffix(".session.lock")
        self.lock_path = self.session.with_suffix(".session.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_release_roundtrip(self):
        """Importable helpers acquire and release the flock correctly."""
        gs = _import_generate_session()

        fd = gs._acquire_session_lock(self.session)
        try:
            second = open(self.lock_path, "w")
            with self.assertRaises(OSError):
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            second.close()
        finally:
            gs._release_session_lock(fd)

        # After release another process must be able to take the lock
        third = open(self.lock_path, "w")
        fcntl.flock(third.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(third.fileno(), fcntl.LOCK_UN)
        third.close()

    def test_refuses_when_lock_held(self):
        """The script exits with guidance when another process holds the lock.

        Runs a COPY of the script in a temp dir with its own config.yaml and
        pre-locked session, so the test never touches the real session or
        the network."""
        import shutil

        work = Path(self.tmp.name)
        (work / "generate_session.py").write_text(SCRIPT.read_text())
        # Minimal config so the script finds creds without the real one;
        # the lock refusal happens before any network access.
        (work / "config.yaml").write_text(
            "heroku_mcp:\n"
            "  api_id: 12345\n"
            "  api_hash: 0123456789abcdef0123456789abcdef\n"
        )
        session_dir = work / "sessions"
        session_dir.mkdir()
        session = session_dir / "heroku_mcp.session"
        session.touch()
        holder = open(session.with_suffix(".session.lock"), "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            r = subprocess.run(
                [PYTHON, str(work / "generate_session.py"), "--phone", "+00000000000"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL, cwd=str(work),
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertNotEqual(r.returncode, 0, "must refuse, not race the lock")
            self.assertIn("Сессия занята", r.stdout)
            self.assertIn("рестарт сервера не нужен", r.stdout)
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()


if __name__ == "__main__":
    unittest.main()
