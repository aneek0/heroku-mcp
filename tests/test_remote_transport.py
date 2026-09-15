"""Remote transport tests: bind host, bearer auth, module URLs.

User request (2026-09-15): the server must be runnable on a remote host and
connectable over HTTP. Two things must not regress while enabling that:
an exposed MCP port has to require a token (fail fast, like `proxy` does for
invalid specs), and `.dlm` URLs must point where the userbot can actually
reach us — the old hardcoded 127.0.0.1 only works when MCP and the userbot
share a machine.
"""

import asyncio
import contextlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mcp.server.transport_security import TransportSecurityMiddleware

from heroku_mcp import config as config_mod
from heroku_mcp import server
from heroku_mcp.config import HerokuMcpSettings, load_settings


class TransportSecurityMiddlewareProbe(TransportSecurityMiddleware):
    """Expose the SDK's (underscore-private) host check under a stable name."""

    def validate_host(self, host: str) -> bool:
        return self._validate_host(host)


def _settings(**overrides) -> HerokuMcpSettings:
    """Settings with every env override stripped so the real config.yaml is out of it."""
    keys = set(overrides) | {"server_host", "auth_token", "module_host", "module_base_url",
                             "allow_unauthenticated", "allowed_hosts"}
    saved = {k: os.environ.pop(f"HEROKU_MCP_{k.upper()}", None) for k in keys}
    try:
        return HerokuMcpSettings(**overrides)
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})


class LoadSettingsRemoteKeys(unittest.TestCase):
    """yaml → env precedence for the new HTTP keys (same path as `proxy`)."""

    def test_yaml_and_env(self):
        for k in ("SERVER_HOST", "AUTH_TOKEN", "MODULE_BASE_URL"):
            os.environ.pop(f"HEROKU_MCP_{k}", None)
        with TemporaryDirectory() as d:
            (Path(d) / "config.yaml").write_text(
                "heroku_mcp:\n"
                "  server_host: 0.0.0.0\n"
                "  auth_token: yaml-token\n"
                "  module_base_url: http://tunnel.invalid:6768\n"
                "  allowed_hosts:\n"
                "    - mcp.invalid\n"
            )
            orig = config_mod._PROJECT_ROOT
            try:
                config_mod._PROJECT_ROOT = Path(d)
                st = load_settings()
                self.assertEqual(st.server_host, "0.0.0.0")
                self.assertEqual(st.auth_token, "yaml-token")
                self.assertEqual(st.module_base_url, "http://tunnel.invalid:6768")
                self.assertEqual(st.allowed_hosts, ["mcp.invalid"])

                os.environ["HEROKU_MCP_AUTH_TOKEN"] = "env-token"
                st = load_settings()
                self.assertEqual(st.auth_token, "env-token")
            finally:
                config_mod._PROJECT_ROOT = orig
                os.environ.pop("HEROKU_MCP_AUTH_TOKEN", None)

    def test_defaults_stay_loopback(self):
        st = _settings()
        self.assertEqual(st.server_host, "127.0.0.1")
        self.assertTrue(st.binds_loopback_only)
        self.assertFalse(st.auth_token)
        # Loopback default must never trip the guard: stdio/local users unaffected.
        st.validate_http_security()


class ValidateHttpSecurity(unittest.TestCase):
    def test_remote_bind_without_token_raises(self):
        st = _settings(server_host="0.0.0.0")
        with self.assertRaises(ValueError) as ctx:
            st.validate_http_security()
        msg = str(ctx.exception)
        self.assertIn("HEROKU_MCP_AUTH_TOKEN", msg)
        self.assertIn("ssh -L", msg)

    def test_remote_bind_with_token_ok(self):
        _settings(server_host="10.0.0.5", auth_token="t").validate_http_security()

    def test_explicit_unauthenticated_ok_but_warns(self):
        st = _settings(server_host="10.0.0.5", allow_unauthenticated=True)
        with self.assertLogs("heroku_mcp.config", level="WARNING") as logs:
            st.validate_http_security()
        self.assertIn("NO authentication", " ".join(logs.output))

    def test_ipv6_bind_is_remote(self):
        with self.assertRaises(ValueError):
            _settings(server_host="fd00::1").validate_http_security()


class ModuleUrl(unittest.TestCase):
    def test_loopback_default_unchanged(self):
        st = _settings()
        self.assertEqual(st.module_url("Tester", 6768), "http://127.0.0.1:6768/Tester.py")

    def test_remote_bind_address_is_used(self):
        st = _settings(server_host="10.1.2.3")
        self.assertEqual(st.module_url("m", 6768), "http://10.1.2.3:6768/m.py")

    def test_module_host_wins_over_server_host(self):
        st = _settings(server_host="10.1.2.3", module_host="127.0.0.1")
        self.assertEqual(st.module_url("m", 6768), "http://127.0.0.1:6768/m.py")

    def test_wildcard_bind_uses_outbound_ip(self):
        st = _settings(server_host="0.0.0.0", auth_token="t")
        with mock.patch.object(config_mod, "detect_local_ip", return_value="203.0.113.9"):
            self.assertEqual(st.module_url("m", 6768), "http://203.0.113.9:6768/m.py")
    def test_base_url_wins_and_strips_trailing_slash(self):
        st = _settings(module_base_url="https://tunnel.invalid:6768/")
        self.assertEqual(st.module_url("m", 6768), "https://tunnel.invalid:6768/m.py")

    def test_ipv6_literal_is_bracketed(self):
        st = _settings(server_host="fd00::1", auth_token="t")
        self.assertEqual(st.module_url("m", 6768), "http://[fd00::1]:6768/m.py")

    def test_base_url_without_scheme_raises(self):
        for bad in ("tunnel.invalid:6768", "ftp://tunnel.invalid"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                self._load_with_base(bad)
            self.assertIn("module_base_url", str(ctx.exception))

    @staticmethod
    def _load_with_base(value):
        for k in ("MODULE_BASE_URL", "SERVER_HOST", "AUTH_TOKEN"):
            os.environ.pop(f"HEROKU_MCP_{k}", None)
        os.environ["HEROKU_MCP_MODULE_BASE_URL"] = value
        try:
            return load_settings()
        finally:
            os.environ.pop("HEROKU_MCP_MODULE_BASE_URL", None)

    def test_public_module_store_warns(self):
        st = _settings(server_host="0.0.0.0", auth_token="t")
        with self.assertLogs("heroku_mcp.config", level="WARNING") as logs:
            st.warn_if_module_store_public()
        self.assertIn("no authentication", " ".join(logs.output))

    def test_module_store_via_base_url_does_not_warn(self):
        st = _settings(server_host="0.0.0.0", auth_token="t",
                       module_base_url="https://tunnel.invalid:6768")
        with mock.patch.object(config_mod.log, "warning") as warn:
            st.warn_if_module_store_public()
        warn.assert_not_called()


class LocalInterfaceIps(unittest.TestCase):
    """A 0.0.0.0 bind is reachable on every interface address — VPN included.

    Regression found while wiring the user's NetBird host (netbo, 100.71.x):
    using only detect_local_ip() left the tunnel address out of the Host
    allowlist, so remote clients over NetBird got 421 on their own connect.
    """

    IP_OUTPUT = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
1: lo    inet6 ::1/128 scope host noprefixroute \\       valid_lft forever
3: wlp3s0    inet 192.168.10.175/24 brd 192.168.10.255 scope global dynamic \\       valid_lft 1s
4: wt0    inet 100.71.96.174/16 brd 100.71.255.255 scope global wt0\\       valid_lft forever
4: wt0    inet6 fd7a:115c:a1e0::1234/128 scope global \\       valid_lft forever
5: tap0    inet6 fe80::42/64 scope link \\       valid_lft forever
"""

    def test_parses_all_unicast_addresses(self):
        with mock.patch.object(config_mod.subprocess, "run", return_value=mock.Mock(stdout=self.IP_OUTPUT)):
            hosts = config_mod.local_interface_ips()
        self.assertIn("127.0.0.1", hosts)
        self.assertIn("192.168.10.175", hosts)
        self.assertIn("100.71.96.174", hosts)          # NetBird/tunnel address
        self.assertIn("[fd7a:115c:a1e0::1234]", hosts)  # bracketed for Host form
        self.assertNotIn("[fe80::42]", hosts)            # link-local skipped

    def test_falls_back_to_outbound_ip_when_ip_missing(self):
        with mock.patch.object(config_mod.subprocess, "run", side_effect=FileNotFoundError), \
                mock.patch.object(config_mod, "detect_local_ip", return_value="203.0.113.7"):
            self.assertEqual(config_mod.local_interface_ips(), ["203.0.113.7"])

    def test_wildcard_bind_accepts_netbird_host(self):
        """The tunnel address must pass even though it is not the outbound one."""
        netbo_ips = ["127.0.0.1", "192.168.10.175", "100.71.96.174", "[fd7a::1]"]
        with mock.patch.object(config_mod, "local_interface_ips", return_value=netbo_ips):
            ts = _settings(server_host="0.0.0.0", auth_token="t").transport_security()
        mw_from = TransportSecurityMiddlewareProbe(ts)
        for h in ("100.71.96.174:6767", "192.168.10.175:6767", "127.0.0.1:6767", "localhost:6767"):
            with self.subTest(h=h):
                self.assertTrue(mw_from.validate_host(h))
        self.assertFalse(mw_from.validate_host("198.51.100.7:6767"))  # foreign address


class TransportSecurity(unittest.TestCase):
    def test_loopback_keeps_localhost_names(self):
        ts = _settings().transport_security()
        self.assertIn("127.0.0.1", ts.allowed_hosts)
        self.assertIn("localhost:*", ts.allowed_hosts)

    def test_remote_bind_host_is_accepted(self):
        ts = _settings(server_host="10.1.2.3", auth_token="t").transport_security()
        self.assertIn("10.1.2.3", ts.allowed_hosts)
        self.assertIn("http://10.1.2.3", ts.allowed_origins)
        # The middleware matches `host:*` for a port, so the wildcard form too.
        self.assertIn("10.1.2.3:*", ts.allowed_hosts)

    def test_allowed_hosts_entries_accepted(self):
        ts = _settings(allowed_hosts=["mcp.invalid", "https://other.invalid"]).transport_security()
        self.assertIn("mcp.invalid", ts.allowed_hosts)
        self.assertIn("other.invalid", ts.allowed_hosts)
        self.assertIn("https://other.invalid:*", ts.allowed_origins)

    def test_middleware_accepts_realistic_remote_request(self):
        """End-to-end through the SDK validator, not just our own lists."""
        ts = _settings(server_host="10.1.2.3", auth_token="t").transport_security()
        mw = TransportSecurityMiddleware(ts)
        self.assertTrue(mw._validate_host("10.1.2.3:6767"))
        self.assertTrue(mw._validate_host("127.0.0.1:6767"))
        # A browser page on another origin must not be able to drive the port.
        self.assertFalse(mw._validate_origin("http://attacker.invalid"))
        self.assertTrue(mw._validate_origin("http://10.1.2.3:6767"))
        self.assertFalse(mw._validate_host("evil.invalid"))


def _run(coro):
    return asyncio.run(coro)


class GuardTest(unittest.TestCase):
    """ASGI edge: 401 for a missing/wrong token, 405 GET, 200 health."""

    def _call(self, scope, token=None):
        calls = []
        reached = {"app": False}

        async def inner(app_scope, receive, send):
            reached["app"] = True

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.start":
                calls.append(message["status"])

        guard = server.Guard(inner)
        _run(guard(scope, receive, send))
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return calls[0] if calls else None, body, reached["app"]

    def _scope(self, method="POST", path="/mcp", headers=None):
        raw = {b"content-type": b"application/json"}
        for k, v in (headers or {}).items():
            raw[k.encode().lower()] = v.encode()
        return {"type": "http", "method": method, "path": path, "headers": list(raw.items())}

    def test_no_token_configured_passes_through(self):
        with mock.patch.object(server.settings, "auth_token", ""):
            status, _, reached = self._call(self._scope())
        self.assertTrue(reached)
        self.assertIsNone(status)

    def test_missing_token_rejected(self):
        with mock.patch.object(server.settings, "auth_token", "s3cret"):
            status, body, reached = self._call(self._scope())
        self.assertEqual(status, 401)
        self.assertIn(b"Unauthorized", body)
        self.assertFalse(reached, "unauthenticated request must not reach the MCP app")

    def test_bearer_and_x_api_key_accepted(self):
        with mock.patch.object(server.settings, "auth_token", "s3cret"):
            for headers in ({"Authorization": "Bearer s3cret"},
                            {"authorization": "bearer s3cret"},
                            {"X-Api-Key": "s3cret"}):
                with self.subTest(headers=headers):
                    status, _, reached = self._call(self._scope(headers=headers))
                    self.assertTrue(reached)
                    self.assertIsNone(status)

    def test_wrong_and_empty_tokens_rejected(self):
        with mock.patch.object(server.settings, "auth_token", "s3cret"):
            for headers in ({"Authorization": "Bearer nope"},
                            {"Authorization": "Bearer "},
                            {"X-Api-Key": ""},
                            {"Authorization": "Basic c2VjcmV0"},
                            {}):
                with self.subTest(headers=headers):
                    self.assertEqual(self._call(self._scope(headers=headers))[0], 401)

    def test_get_rejected_except_health(self):
        with mock.patch.object(server.settings, "auth_token", "s3cret"):
            self.assertEqual(self._call(self._scope("GET", "/mcp"))[0], 405)
            for path in ("/healthz", "/health"):
                status, body, reached = self._call(self._scope("GET", path))
                self.assertEqual(status, 200)
                self.assertEqual(body, b"ok")
                self.assertFalse(reached)

    def test_health_probe_needs_no_token(self):
        with mock.patch.object(server.settings, "auth_token", ""):
            self.assertEqual(self._call(self._scope("GET", "/healthz"))[0], 200)


class HttpAppLifespan(unittest.TestCase):
    """HTTP mode must run our startup, not only the SDK's session manager.

    FastMCP.streamable_http_app() overwrites the Starlette lifespan with
    session_manager.run(), which silently skipped our module-store startup and
    Telegram pre-sync in HTTP mode while stdio ran them.
    """

    def test_chained_lifespan_runs_both(self):
        events: list[str] = []

        @contextlib.asynccontextmanager
        async def our_lifespan(app):
            events.append("ours-enter")
            yield
            events.append("ours-exit")

        async def sdk_lifespan(scope_app):
            events.append("sdk-enter")
            yield
            events.append("sdk-exit")

        fake_app = mock.Mock()
        # Starlette stores router.lifespan_context as a callable returning an
        # async CM, not as a bare async generator function.
        fake_app.router.lifespan_context = contextlib.asynccontextmanager(sdk_lifespan)

        async def drive():
            async with built.router.lifespan_context(fake_app):
                pass

        with mock.patch.object(server.mcp, "streamable_http_app", return_value=fake_app), \
                mock.patch.object(server, "_lifespan_ctx", our_lifespan):
            built = server.build_http_app()
            asyncio.run(drive())
        self.assertEqual(events, ["ours-enter", "sdk-enter", "sdk-exit", "ours-exit"])


class ModuleStoreBind(unittest.TestCase):
    def test_binds_configured_host(self):
        from heroku_mcp import module_store

        captured = []

        class FakeSite:
            def __init__(self, runner, host, port):
                captured.append((host, port))

            async def start(self):
                return None

        with mock.patch.object(module_store, "_runner", None), \
                mock.patch.object(module_store, "_bound_port", None), \
                mock.patch.object(module_store.web, "AppRunner", return_value=mock.AsyncMock()), \
                mock.patch.object(module_store.web, "TCPSite", FakeSite), \
                mock.patch.object(module_store.settings, "server_port", 6767), \
                mock.patch.object(module_store.settings, "module_host", "10.1.2.3"), \
                mock.patch.object(module_store.settings, "server_host", "10.1.2.3"):
            _run(module_store.start_server())
            self.assertEqual(captured[0], ("10.1.2.3", 6768))
            self.assertEqual(module_store.get_bound_port(), 6768)
            _run(module_store.stop_server())


if __name__ == "__main__":
    unittest.main()
