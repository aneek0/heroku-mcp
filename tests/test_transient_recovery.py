"""Transient Telegram error recovery tests.

Found by live E2E testing (2026-09-12): the user's MTProto proxy cluster
rotated mid-session and every call failed with RPCError 400
MTPROTO_CLUSTER_INVALID for 15+ minutes. Tools have no retry path:

* get_history/get_history_json called client.get_messages() directly, so the
  RPCError surfaced to the MCP client on the very first hit.
* send_command's poll loop swallowed the error via ``except Exception: pass``
  and hung until the tool timeout with zero diagnostics.

The fix: _is_transient() classifies stale-cluster errors (Telethon 1.44 has
no dedicated class for MTPROTO_CLUSTER_INVALID — it deserializes as a generic
RPCError, so the message string is matched), and _call_with_recovery()
reconnects and retries once.
"""

import asyncio
import unittest
from unittest import mock

from telethon import errors as tg_errors

from heroku_mcp import telegram


def _run(coro):
    return asyncio.run(coro)


def _make_rpc_error(msg="MTPROTO_CLUSTER_INVALID", code=400):
    err = tg_errors.RPCError(request=None, message=msg, code=code)
    return err


class IsTransientTest(unittest.TestCase):
    def test_cluster_invalid_rpc_error_is_transient(self):
        self.assertTrue(telegram._is_transient(_make_rpc_error()))

    def test_generic_cluster_invalid_string_is_transient(self):
        # Telethon may raise non-RPCError wrappers; message match still fires.
        self.assertTrue(
            telegram._is_transient(ValueError("MTPROTO_CLUSTER_INVALID"))
        )

    def test_connection_and_timeout_errors_are_transient(self):
        self.assertTrue(telegram._is_transient(ConnectionError("reset")))
        self.assertTrue(telegram._is_transient(TimeoutError("t")))
        self.assertTrue(telegram._is_transient(asyncio.TimeoutError()))

    def test_normal_rpc_errors_are_not_transient(self):
        # e.g. 420 FLOOD_WAIT — retrying with a reconnect won't help.
        err = tg_errors.RPCError(request=None, message="FLOOD_WAIT_30", code=420)
        self.assertFalse(telegram._is_transient(err))

    def test_value_error_without_cluster_marker_is_not_transient(self):
        self.assertFalse(telegram._is_transient(ValueError("Could not find the input entity")))


class CallWithRecoveryTest(unittest.TestCase):
    def test_retries_after_transient_error_and_reconnects(self):
        client = mock.MagicMock()
        client.get_messages = mock.AsyncMock(
            side_effect=[
                _make_rpc_error(),   # attempt 1: stale cluster
                mock.sentinel.MSGS,  # attempt 2: success after reconnect
            ]
        )
        reconnect_calls = []

        async def fake_reset():
            reconnect_calls.append(True)

        with (
            mock.patch.object(telegram, "_reset_client", fake_reset),
            mock.patch.object(telegram, "_is_transient", return_value=True),
        ):
            result = _run(
                telegram._call_with_recovery(
                    lambda: client.get_messages("me", limit=5)
                )
            )

        self.assertIs(result, mock.sentinel.MSGS)
        self.assertEqual(client.get_messages.await_count, 2)
        self.assertEqual(len(reconnect_calls), 1, "must reconnect exactly once between attempts")

    def test_non_transient_error_raises_immediately(self):
        client = mock.MagicMock()
        client.get_messages = mock.AsyncMock(side_effect=ValueError("nope"))

        async def fail_reset():
            raise AssertionError("must not reconnect on non-transient errors")

        with (
            mock.patch.object(telegram, "_reset_client", fail_reset),
        ):
            with self.assertRaises(ValueError):
                _run(
                    telegram._call_with_recovery(
                        lambda: client.get_messages("me", limit=5)
                    )
                )
        self.assertEqual(client.get_messages.await_count, 1)

    def test_gives_up_after_max_attempts(self):
        client = mock.MagicMock()
        client.get_messages = mock.AsyncMock(
            side_effect=_make_rpc_error()
        )

        async def fake_reset():
            pass

        with mock.patch.object(telegram, "_reset_client", fake_reset):
            with self.assertRaises(tg_errors.RPCError):
                _run(
                    telegram._call_with_recovery(
                        lambda: client.get_messages("me", limit=5),
                        attempts=3,
                    )
                )
        self.assertEqual(client.get_messages.await_count, 3)


if __name__ == "__main__":
    unittest.main()
