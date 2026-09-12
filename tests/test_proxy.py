"""Unit tests for proxy config parsing (offline, no network).

Run:  .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# config.py loads settings at import time from cwd/config.yaml — run from
# a neutral directory so tests don't depend on the local config.
os.environ.setdefault("HEROKU_MCP_API_ID", "1")
os.environ.setdefault("HEROKU_MCP_API_HASH", "0" * 32)

from heroku_mcp.proxy import parse_proxy, proxy_description  # noqa: E402


class ParseProxyMTProto(unittest.TestCase):
    def test_task_link_dd_secret(self):
        """The exact link from the task request: dd-secret -> RandomizedIntermediate."""
        k = parse_proxy(
            "tg://proxy?server=203.0.113.1&port=1443&secret=dd00112233445566778899aabbccddee1"
        )
        self.assertEqual(
            k["proxy"], ("203.0.113.1", 1443, "dd00112233445566778899aabbccddee1")
        )
        self.assertEqual(
            k["connection"].__name__, "ConnectionTcpMTProxyRandomizedIntermediate"
        )

    def test_plain_hex_secret_intermediate(self):
        k = parse_proxy("tg://proxy?server=1.2.3.4&port=443&secret=" + "ab" * 16)
        self.assertEqual(k["connection"].__name__, "ConnectionTcpMTProxyIntermediate")

    def test_ee_secret_abridged(self):
        k = parse_proxy(
            "https://t.me/proxy?server=proxy.example.com&port=443&secret=eeabcdef0123456789"
        )
        self.assertEqual(k["connection"].__name__, "ConnectionTcpMTProxyAbridged")
        self.assertEqual(k["proxy"][0], "proxy.example.com")

    def test_schemeless_tme_link(self):
        k = parse_proxy(
            "t.me/proxy?server=proxy.example.com&port=443&secret=eeabcdef0123456789"
        )
        self.assertEqual(k["connection"].__name__, "ConnectionTcpMTProxyAbridged")

    def test_extra_query_params_ignored(self):
        k = parse_proxy(
            "tg://proxy?server=1.2.3.4&port=443&secret=" + "cd" * 16 + "&extra=1"
        )
        self.assertEqual(k["proxy"], ("1.2.3.4", 443, "cd" * 16))

    def test_base64_secret_with_plus_and_slash_preserved(self):
        """Base64 secrets legitimately contain '+' and '/'.

        urllib's parse_qs/parse_qsl decode '+' as space (form encoding),
        silently corrupting such secrets. The parser must preserve the
        raw value byte-for-byte, whether raw or percent-encoded.
        """
        secret = "dd" + "u130d2ihiw+hPDYb4h1Oxg=="  # real b64 payload w/ '+' and '/'
        k = parse_proxy(f"tg://proxy?server=1.2.3.4&port=443&secret={secret}")
        self.assertEqual(k["proxy"][2], secret)

        from urllib.parse import quote

        k2 = parse_proxy(
            f"tg://proxy?server=1.2.3.4&port=443&secret={quote(secret)}"
        )
        self.assertEqual(k2["proxy"][2], secret)

    def test_empty_server_param_rejected(self):
        with self.assertRaises(ValueError):
            parse_proxy("tg://proxy?server=&port=443&secret=" + "ab" * 16)


class ParseProxySocksHttp(unittest.TestCase):
    def test_socks5_with_auth(self):
        k = parse_proxy("socks5://user:pass@127.0.0.1:1080")
        self.assertEqual(
            k,
            {
                "proxy": {
                    "proxy_type": "socks5",
                    "addr": "127.0.0.1",
                    "port": 1080,
                    "rdns": True,
                    "username": "user",
                    "password": "pass",
                }
            },
        )

    def test_socks4_and_http_no_auth(self):
        self.assertEqual(
            parse_proxy("socks4://10.0.0.1:1080"),
            {"proxy": {"proxy_type": "socks4", "addr": "10.0.0.1", "port": 1080, "rdns": True}},
        )
        self.assertEqual(
            parse_proxy("http://10.0.0.2:3128"),
            {"proxy": {"proxy_type": "http", "addr": "10.0.0.2", "port": 3128, "rdns": True}},
        )

    def test_tg_socks_link(self):
        k = parse_proxy("tg://socks?server=5.6.7.8&port=9999&user=u&pass=p")
        self.assertEqual(
            k,
            {
                "proxy": {
                    "proxy_type": "socks5",
                    "addr": "5.6.7.8",
                    "port": 9999,
                    "rdns": True,
                    "username": "u",
                    "password": "p",
                }
            },
        )
        self.assertEqual(
            parse_proxy("https://t.me/socks?server=5.6.7.8&port=9999"),
            {"proxy": {"proxy_type": "socks5", "addr": "5.6.7.8", "port": 9999, "rdns": True}},
        )


class ParseProxyInvalid(unittest.TestCase):
    def test_empty_is_disabled(self):
        self.assertEqual(parse_proxy(""), {})
        self.assertEqual(parse_proxy("   "), {})

    def test_invalid_raises(self):
        bad = [
            "garbage",  # unknown scheme
            "tg://proxy?server=1.2.3.4",  # missing port+secret
            "tg://proxy?server=1.2.3.4&port=abc&secret=" + "ab" * 16,  # bad port
            "tg://proxy?server=1.2.3.4&port=0&secret=" + "ab" * 16,  # port range
            "tg://proxy?server=1.2.3.4&port=70000&secret=" + "ab" * 16,  # port range
            "t.me/proxy?server=1.2.3.4&port=1&secret=ee00",  # short secret
            "socks5://host",  # missing port
            "ftp://host:21",  # unsupported scheme
            "tg://http?server=1.2.3.4&port=1",  # unsupported tg:// kind
        ]
        for spec in bad:
            with self.assertRaises(ValueError, msg=spec):
                parse_proxy(spec)


class ProxyDescription(unittest.TestCase):
    def test_never_leaks_secret_or_password(self):
        self.assertEqual(
            proxy_description(
                "tg://proxy?server=203.0.113.1&port=1443&secret=dd00112233445566778899aabbccddee1"
            ),
            "mtproto 203.0.113.1:1443",
        )
        self.assertEqual(proxy_description(""), "direct connection")
        desc = proxy_description("socks5://user:pass@127.0.0.1:1080")
        self.assertNotIn("pass", desc)
        self.assertIn("127.0.0.1:1080", desc)


class ClientConstruction(unittest.TestCase):
    def test_telegram_client_accepts_all_proxy_kinds(self):
        """TelegramClient(**kwargs) constructs cleanly (python-socks installed, no warnings)."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            from telethon import TelegramClient

            with TemporaryDirectory() as d:
                for spec in [
                    "tg://proxy?server=203.0.113.1&port=1443&secret=dd00112233445566778899aabbccddee1",
                    "socks5://user:pass@127.0.0.1:1080",
                    "http://10.0.0.2:3128",
                ]:
                    client = TelegramClient(
                        str(Path(d) / "s"), 1, "0" * 32, **parse_proxy(spec)
                    )
                    self.assertIsNotNone(client)


class SettingsProxy(unittest.TestCase):
    """yaml → env precedence for the proxy key.

    _load_yaml_config() checks _PROJECT_ROOT/config.yaml first, then cwd —
    so the test patches _PROJECT_ROOT to a temp dir and reloads the module.
    """

    def test_yaml_value_and_env_override(self):
        os.environ.pop("HEROKU_MCP_PROXY", None)
        with TemporaryDirectory() as d:
            (Path(d) / "config.yaml").write_text(
                'heroku_mcp:\n  proxy: "socks5://yamlhost:1080"\n  api_id: 111\n'
            )
            orig_root = None
            try:
                import heroku_mcp.config as config

                orig_root = config._PROJECT_ROOT
                config._PROJECT_ROOT = Path(d)
                st = config.load_settings()
                self.assertEqual(st.proxy, "socks5://yamlhost:1080")

                os.environ["HEROKU_MCP_PROXY"] = "http://envhost:3128"
                st = config.load_settings()
                self.assertEqual(st.proxy, "http://envhost:3128")
            finally:
                if orig_root is not None:
                    import heroku_mcp.config as config

                    config._PROJECT_ROOT = orig_root
                os.environ.pop("HEROKU_MCP_PROXY", None)


if __name__ == "__main__":
    unittest.main()
