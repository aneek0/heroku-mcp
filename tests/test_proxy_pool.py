"""Proxy pool tests.

E2E finding (2026-09-12): the user's local MTProto proxy cluster died
mid-session (RPCError 400 MTPROTO_CLUSTER_INVALID for 15+ min). The fix is a
proxy pool: proxy_list_url points to a text file with one tg://proxy link
per line; connect cycles round-robin until a handshake succeeds, and a stale
pool triggers a refetch on the next reconnect.

Uses only synthetic addresses from TEST-NET-1 (203.0.113.0/24, RFC 5737) —
no real proxy secrets.
"""

import asyncio
import unittest
from unittest import mock

from heroku_mcp import telegram
from heroku_mcp.proxy import parse_proxy_list


SAMPLE_LIST = """\
# comment line
tg://proxy?server=203.0.113.1&port=443&secret=ddb8cf2eTESTTESTTESTTESTTESTTEST
tg://proxy?server=203.0.113.2&port=443&secret=ddb8cf2eTESTTESTTESTTESTTESTTEST

not a proxy line
socks5://203.0.113.3:1080
"""


class ParseProxyListTest(unittest.TestCase):
    def test_extracts_valid_and_skips_junk(self):
        proxies = parse_proxy_list(SAMPLE_LIST)
        self.assertEqual(len(proxies), 3)
        self.assertTrue(proxies[0].startswith("tg://proxy?server=203.0.113.1"))
        self.assertTrue(proxies[2].startswith("socks5://"))

    def test_empty_text(self):
        self.assertEqual(parse_proxy_list(""), [])


def _run(coro):
    return asyncio.run(coro)


class ProxyPoolTest(unittest.TestCase):
    def setUp(self):
        # Reset module state between tests
        telegram._proxy_pool = []
        telegram._pool_index = 0
        telegram._pool_loaded = False
        telegram._current_proxy = None

    def _fake_client(self):
        c = mock.MagicMock()
        c.connect = mock.AsyncMock()
        c.disconnect = mock.AsyncMock()
        return c

    def test_pool_cycles_until_proxy_connects(self):
        telegram._proxy_pool = ["p1", "p2", "p3"]
        telegram._pool_loaded = True
        clients = [self._fake_client() for _ in range(3)]
        # p1 and p2 fail, p3 works
        clients[0].connect = mock.AsyncMock(side_effect=ConnectionError("down"))
        clients[1].connect = mock.AsyncMock(side_effect=ConnectionError("down"))

        with (
            mock.patch.object(telegram, "_build_client", side_effect=clients),
            mock.patch.object(telegram, "proxy_description", side_effect=lambda s: f"mock {s}"),
        ):
            result = _run(telegram._connect_pool())

        self.assertIs(result, clients[2])
        self.assertEqual(telegram._pool_index, 0, "round-robin must advance past the working proxy")
        self.assertEqual(clients[0].disconnect.await_count, 1)
        self.assertEqual(clients[1].disconnect.await_count, 1)

    def test_pool_all_fail_falls_back_to_legacy_path(self):
        telegram._proxy_pool = ["p1"]
        telegram._pool_loaded = True
        fail = self._fake_client()
        fail.connect = mock.AsyncMock(side_effect=ConnectionError("down"))

        legacy = self._fake_client()

        def build(spec=None):
            return fail if spec == "p1" else legacy

        async def legacy_connect(client):
            client.connect.awaited = True

        with (
            mock.patch.object(telegram, "_build_client", side_effect=build),
            mock.patch.object(telegram, "_connect_with_retries", side_effect=legacy_connect),
            mock.patch.object(telegram, "proxy_description", side_effect=lambda s: f"mock {s}"),
        ):
            result = _run(telegram._connect_pool())

        self.assertIs(result, legacy)
        self.assertFalse(telegram._pool_loaded, "stale pool must be refetched next time")

    def test_load_pool_fetches_url_and_merges_primary_proxy(self):
        telegram._proxy_pool = []
        telegram._pool_loaded = False

        fetched = "tg://proxy?server=203.0.113.9&port=443&secret=ddb8cf2eTESTTESTTESTTESTTESTTEST"
        primary = "tg://proxy?server=203.0.113.1&port=443&secret=ddb8cf2eTESTTESTTESTTESTTESTTEST"

        settings = mock.MagicMock()
        settings.proxy_list_url = "https://example.invalid/list.txt"
        settings.proxy = primary

        with (
            mock.patch.object(telegram, "settings", settings),
            mock.patch("urllib.request.urlopen") as urlopen,
        ):
            m = mock.MagicMock()
            m.__enter__ = mock.MagicMock(return_value=mock.MagicMock(read=lambda: fetched.encode()))
            m.__exit__ = mock.MagicMock(return_value=False)
            urlopen.return_value = m
            telegram._load_proxy_pool()

        # Primary proxy goes first (index 0), fetched list follows
        self.assertEqual(telegram._proxy_pool, [primary, fetched])
        self.assertTrue(telegram._pool_loaded)

    def test_pool_fetch_failure_falls_back_to_primary(self):
        telegram._proxy_pool = []
        telegram._pool_loaded = False
        primary = "tg://proxy?server=203.0.113.1&port=443&secret=ddb8cf2eTESTTESTTESTTESTTESTTEST"

        settings = mock.MagicMock()
        settings.proxy_list_url = "https://example.invalid/list.txt"
        settings.proxy = primary

        with (
            mock.patch.object(telegram, "settings", settings),
            mock.patch("urllib.request.urlopen", side_effect=OSError("net down")),
        ):
            telegram._load_proxy_pool()

        self.assertEqual(telegram._proxy_pool, [primary])
        self.assertTrue(telegram._pool_loaded)


if __name__ == "__main__":
    unittest.main()
