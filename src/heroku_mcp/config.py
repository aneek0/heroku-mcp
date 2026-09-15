"""Configuration management for Heroku MCP Server."""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Union

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Bind addresses that accept connections from anywhere: they cannot be turned
# into a URL for the userbot without picking a real interface address.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*", "[::]"})
# Bind addresses only this machine can reach — no token needed there.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# t.me/c/<internal-channel-id>/<topic-or-message-id>
# t.me/<username>/<topic-or-message-id>
_PRIVATE_LINK_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)/(\d+)/?$",
    re.IGNORECASE,
)
_PUBLIC_LINK_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]{3,64})/(\d+)/?$",
    re.IGNORECASE,
)


def parse_chat_link(link: str) -> tuple[Union[str, int], int]:
    """Extract (chat_id, topic_id) from a Telegram topic/message link.

    Accepted forms:
    - ``https://t.me/c/3537976236/7``  -> (3537976236, 7)
      private supergroup/channel link: /c/<id>/<topic or msg id>
    - ``https://t.me/somegroup/7``     -> ("somegroup", 7)
      public link: /<username>/<topic or msg id>

    A plain chat id / username / "me" without a trailing /<id> is passed
    through unchanged with topic 0 (no topic).

    Raises ValueError for malformed links.
    """
    link = (link or "").strip()
    if not link:
        return "me", 0

    m = _PRIVATE_LINK_RE.match(link)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = _PUBLIC_LINK_RE.match(link)
    if m:
        return m.group(1), int(m.group(2))

    # Plain username or numeric id without topic — pass through.
    lowered = link.lower()
    if lowered.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        rest = link.split("t.me/", 1)[1].strip("/")
        if "/" in rest:
            raise ValueError(
                f"Unrecognized t.me link format: {link!r}; expected "
                "t.me/c/<id>/<topic> or t.me/<username>/<topic>"
            )
        return rest, 0

    # Plain chat link like https://t.me/<username> handled above; numeric or
    # "me" or @username pass through.
    if link.startswith("@"):
        return link[1:], 0
    if link.lstrip("-").isdigit():
        return int(link), 0
    if link == "me":
        return "me", 0
    return link, 0


class HerokuMcpSettings(BaseSettings):
    """Settings loaded from environment or config.yaml."""

    api_id: int = Field(default=0)
    api_hash: str = Field(default="")
    session_path: str = Field(default="sessions/heroku_mcp")
    server_port: int = Field(default=6767)
    server_host: str = Field(
        default="127.0.0.1",
        description="Interface the streamable HTTP transport binds to. Loopback "
        "keeps the server local; any other value is a remote bind and needs "
        "`auth_token` (see validate_http_security)",
    )
    auth_token: str = Field(
        default="",
        description="Bearer token required on every HTTP request (Authorization: "
        "Bearer <token> or X-Api-Key). Empty = auth disabled",
    )
    allow_unauthenticated: bool = Field(
        default=False,
        description="Permit a non-loopback bind with no auth_token. Only for a "
        "trusted network: whoever reaches the port controls the userbot",
    )
    allowed_hosts: list[str] = Field(
        default=[],
        description="Extra Host/Origin values accepted by DNS-rebinding "
        "protection on a loopback bind — e.g. a tunnel domain",
    )
    module_host: str = Field(
        default="",
        description="Interface the module (.dlm) HTTP server binds to. Empty = "
        "same as server_host. It serves raw .py sources with no auth, so on a "
        "public bind prefer module_base_url through a tunnel",
    )
    module_base_url: str = Field(
        default="",
        description="Base URL the userbot fetches module sources from, e.g. "
        "https://tunnel.example. Empty = derived from the bind host",
    )
    her_chat_id: Union[str, int] = Field(default="me")
    modules_dir: str = Field(default="modules")
    her_topic_id: int = Field(default=0)
    restart_boot_wait: int = Field(default=30)
    proxy: str = Field(default="")
    proxy_list_url: str = Field(
        default="",
        description="URL (http/https) of a text file with one proxy link per line; "
        "the pool cycles through working proxies. Falls back to `proxy` when empty",
    )
    proxy_check_timeout: float = Field(
        default=10.0,
        description="Per-proxy MTProto handshake timeout when cycling the pool",
    )

    @property
    def modules_path(self) -> Path:
        p = Path(self.modules_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def binds_loopback_only(self) -> bool:
        return self.server_host in _LOOPBACK_HOSTS

    @property
    def module_bind_host(self) -> str:
        return self.module_host or self.server_host

    def validate_http_security(self) -> None:
        """Refuse to expose the MCP port without a token.

        The streamable HTTP transport has no identity of its own: an open port
        is an open userbot (every tool ends up running commands as you). A
        loopback bind is safe as before; a remote one needs `auth_token`, or an
        explicit `allow_unauthenticated` for a trusted network.
        """
        if self.binds_loopback_only or self.auth_token:
            return
        if self.allow_unauthenticated:
            log.warning(
                "Serving MCP on %s:%d with NO authentication "
                "(allow_unauthenticated). Anyone who can reach the port can run "
                "userbot commands.",
                self.server_host, self.server_port,
            )
            return
        raise ValueError(
            f"server_host {self.server_host!r} exposes the MCP port to the "
            f"network, but auth_token is empty. Set HEROKU_MCP_AUTH_TOKEN "
            f"(clients then send 'Authorization: Bearer <token>'), or keep "
            f"server_host on 127.0.0.1 and tunnel in with "
            f"'ssh -L 6767:127.0.0.1:6767 <host>', or set "
            f"allow_unauthenticated=true if you really mean it."
        )

    def module_url(self, name: str, port: int) -> str:
        """URL handed to the userbot for `.dlm`.

        `module_base_url` wins (a tunnel or reverse-proxy name). Otherwise the
        bind host is used, because that is what the userbot can reach — except
        for wildcard binds, where we ask the kernel which interface would be
        used to reach the internet.
        """
        if self.module_base_url:
            return f"{self.module_base_url}/{name}.py"
        host = self.module_bind_host
        if host in _WILDCARD_HOSTS:
            host = detect_local_ip()
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6 literals need brackets in a URL
        # No TLS here: a public listener is expected to sit behind a reverse
        # proxy or tunnel, which is what module_base_url is for.
        return f"http://{host}:{port}/{name}.py"

    def _host_bases(self) -> list[str]:
        """Host names the MCP endpoint should accept in Host/Origin headers.

        FastMCP auto-enables DNS-rebinding protection with loopback-only
        allowed hosts, so a remote bind (or a tunnel domain) is rejected with
        421 unless the names are listed here. Entries may be given with or
        without a port; the matcher accepts `h:*` as a port wildcard.
        """
        bases = ["127.0.0.1", "localhost", "[::1]"]
        bind = self.server_host or "127.0.0.1"
        if bind in _WILDCARD_HOSTS:
            # A wildcard bind answers on every address of this box — including
            # VPN/tunnel ones — so all of them must pass the Host check.
            bases.extend(local_interface_ips())
        elif bind not in _LOOPBACK_HOSTS:
            bases.append(f"[{bind}]" if ":" in bind and not bind.startswith("[") else bind)
        for raw in self.allowed_hosts:
            h = raw.strip()
            if not h:
                continue
            if "://" in h:  # tolerate a full origin value
                h = h.split("://", 1)[1]
            bases.append(h[:-2] if h.endswith(":*") else h)
        out: list[str] = []
        for h in bases:
            if h and h not in out:
                out.append(h)
        return out

    def transport_security(self):
        """TransportSecuritySettings for the streamable HTTP app.

        Without this, binding to a remote interface breaks at request time:
        FastMCP's automatic protection only allows 127.0.0.1/localhost.
        """
        from mcp.server.transport_security import TransportSecuritySettings

        bases = self._host_bases()
        hosts: list[str] = []
        origins: list[str] = []
        for h in bases:
            hosts.extend((h, f"{h}:*"))
            origins.extend((f"http://{h}", f"http://{h}:*", f"https://{h}", f"https://{h}:*"))
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    def warn_if_module_store_public(self) -> None:
        """Log once at startup if module sources become reachable off-box.

        The module store has no auth and serves the plain source of every
        module in `modules_dir` (which can contain hardcoded tokens), so a
        non-loopback bind deserves a warning even though it is often what
        makes remote `.dlm` work.
        """
        bind = self.module_bind_host
        if self.module_base_url or bind in _LOOPBACK_HOSTS:
            return
        log.warning(
            "Module store is reachable from the network on %s:%d and has no "
            "authentication — module sources under %s can be downloaded by "
            "anyone who can connect. Set module_base_url to a tunnel/reverse-"
            "proxy address instead, or module_host to 127.0.0.1 if the "
            "userbot fetches sources some other way.",
            bind, self.server_port + 1, self.modules_dir,
        )

    model_config = {"env_prefix": "HEROKU_MCP_"}

    @field_validator("server_host", "module_host", "auth_token", mode="before")
    @classmethod
    def _strip(cls, value):
        return str(value).strip() if value is not None else ""

    @field_validator("module_base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value):
        """Trim and require scheme; the URL is handed to the userbot verbatim."""
        base = str(value or "").strip().rstrip("/")
        if base and not base.lower().startswith(("http://", "https://")):
            raise ValueError(
                f"module_base_url must be an http(s) URL, got {value!r}"
            )
        return base


def detect_local_ip() -> str:
    """Primary outbound IPv4 of this machine.

    A UDP socket `connect()` only pins the routing entry, no packet leaves,
    so this works with no network peers and in tests.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


_addr_line = re.compile(r"\binet6?\s+(\S+)")


def local_interface_ips() -> list[str]:
    """Every unicast address assigned to this machine, as Host-header forms.

    Needed because a wildcard bind (`0.0.0.0`) is reachable on any of them —
    a VPN/tunnel address like a NetBird `100.x` one included, which is not the
    address `detect_local_ip()` would report. IPv6 gets the bracket form so the
    values match what clients actually send in `Host`. Returns the outbound
    address alone if `ip` is unavailable (no iproute2 in some containers).
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except Exception as e:  # missing binary, timeout, non-zero exit
        log.debug("Could not list interface addresses (%s), using outbound IP", e)
        return [detect_local_ip()]
    hosts: list[str] = []
    for raw in _addr_line.findall(out):
        addr = raw.split("/", 1)[0].split("%", 1)[0]
        if addr.startswith("169.254.") or addr.lower().startswith("fe80:"):
            continue  # link-local: not a usable Host value
        if ":" in addr:
            addr = f"[{addr}]"
        if addr not in hosts:
            hosts.append(addr)
    if "127.0.0.1" not in hosts:
        hosts.insert(0, "127.0.0.1")
    return hosts


def _load_yaml_config() -> dict:
    """Try to load config.yaml from project root."""
    for candidate in [
        _PROJECT_ROOT / "config.yaml",
        Path.cwd() / "config.yaml",
    ]:
        if candidate.is_file():
            with open(candidate) as f:
                data = yaml.safe_load(f) or {}
            return data.get("heroku_mcp", {})
    return {}


def _resolve_project_path(raw: str) -> str:
    """Resolve a path relative to PROJECT_ROOT if it's not absolute."""
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str(_PROJECT_ROOT / p)


def load_settings() -> HerokuMcpSettings:
    """Build settings from YAML defaults + env vars (env wins)."""
    yaml_values = _load_yaml_config()
    env_overrides = {}
    for key in yaml_values:
        env_key = f"HEROKU_MCP_{key.upper()}"
        if env_key in os.environ:
            env_overrides[key] = os.environ[env_key]
    merged = {**yaml_values, **env_overrides}
    settings = HerokuMcpSettings(**merged)
    # Resolve relative paths against project root
    settings.session_path = _resolve_project_path(settings.session_path)
    settings.modules_dir = _resolve_project_path(settings.modules_dir)
    settings.server_host = str(settings.server_host).strip()
    settings.module_host = str(settings.module_host).strip()
    settings.auth_token = str(settings.auth_token).strip()
    # Accept a t.me topic link as her_chat_id: extract chat + topic.
    chat, topic = parse_chat_link(str(settings.her_chat_id))
    settings.her_chat_id = chat
    if not settings.her_topic_id:  # explicit her_topic_id wins over the link
        settings.her_topic_id = topic
    return settings


settings = load_settings()
