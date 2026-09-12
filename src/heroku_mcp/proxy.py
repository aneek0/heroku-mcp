"""Proxy configuration parsing for the Telethon client.

Supports:
- MTProto proxy links: ``tg://proxy?server=...&port=...&secret=...``
  (same format as ``https://t.me/proxy?...``), including ``dd``/``ee``
  padded secrets.
- SOCKS4/SOCKS5/HTTP proxy URIs: ``socks5://[user:pass@]host:port``,
  ``socks4://host:port``, ``http://host:port``.
- ``tg://socks?...`` / ``https://t.me/socks?...`` links (SOCKS5).

Returns kwargs suitable for ``TelegramClient(session, api_id, api_hash, **kwargs)``.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from telethon.network.connection import (
    ConnectionTcpMTProxyAbridged,
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)

# Secret prefix -> MTProto connection mode used by Telethon.
# See telethon.network.connection.tcpmtproxy (packet codecs).
_MTProto_CONNECTIONS = {
    "dd": ConnectionTcpMTProxyRandomizedIntermediate,
    "ee": ConnectionTcpMTProxyAbridged,
    "": ConnectionTcpMTProxyIntermediate,
}

_SOCKS_SCHEMES = {"socks5", "socks4", "http"}


def _query_params(query: str) -> dict[str, str]:
    """Parse query params WITHOUT form-style '+'-to-space decoding.

    tg://proxy links embed base64 secrets that legitimately contain '+' and
    '/'. urllib's parse_qs/parse_qsl treat '+' as space (form encoding),
    silently corrupting such secrets into connection failures.
    """
    params = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        params[unquote(key)] = unquote(value)
    return params


def _parse_mtproto(params: dict[str, str], spec: str) -> dict:
    server = params.get("server", "").strip()
    port = params.get("port", "").strip()
    secret = params.get("secret", "").strip()

    if not server or not port or not secret:
        raise ValueError(
            "MTProto proxy link must contain server, port and secret, "
            f"got: {spec!r}"
        )
    try:
        port_num = int(port)
    except ValueError:
        raise ValueError(f"MTProto proxy port must be a number, got {port!r}") from None
    if not (0 < port_num < 65536):
        raise ValueError(f"MTProto proxy port out of range: {port_num}")
    body = secret[2:] if secret[:2].lower() in ("dd", "ee") else secret
    if len(body) < 16:
        raise ValueError(
            f"MTProto proxy secret looks too short ({len(body)} bytes): {spec!r}"
        )

    prefix = secret[:2].lower() if secret[:2].lower() in ("dd", "ee") else ""
    connection = _MTProto_CONNECTIONS[prefix]
    # Telethon normalizes the secret itself (strips dd/ee, decodes hex/base64).
    return {"connection": connection, "proxy": (server, port_num, secret)}


def _parse_socks_uri(spec: str) -> dict:
    parts = urlsplit(spec)
    scheme = parts.scheme.lower()
    if scheme not in _SOCKS_SCHEMES:
        raise ValueError(
            f"Unknown proxy scheme {parts.scheme!r} in {spec!r}; expected one of "
            "tg://proxy, t.me/proxy, socks5://, socks4://, http://"
        )

    host = parts.hostname
    if not host:
        raise ValueError(f"Proxy host missing in {spec!r}")
    if parts.port is None:
        raise ValueError(f"Proxy port missing in {spec!r}")

    proxy: dict = {
        "proxy_type": scheme,
        "addr": host,
        "port": parts.port,
        "rdns": True,
    }
    if parts.username or parts.password:
        proxy["username"] = parts.username or ""
        proxy["password"] = parts.password or ""
    return {"proxy": proxy}


def parse_proxy(spec: str) -> dict:
    """Parse a proxy spec string into TelegramClient kwargs.

    Args:
        spec: Proxy string — MTProto link (``tg://proxy?...`` /
            ``https://t.me/proxy?...``), SOCKS/HTTP URI, or empty.

    Returns:
        Dict with ``connection`` and/or ``proxy`` kwargs for TelegramClient;
        empty dict when ``spec`` is empty (proxy disabled).

    Raises:
        ValueError: If the spec cannot be parsed. The caller should fail
            fast instead of silently connecting without a proxy.
    """
    spec = (spec or "").strip()
    if not spec:
        return {}

    lower = spec.lower()
    if lower.startswith("tg://") or "t.me/" in lower:
        # Accept schemeless "t.me/proxy?..." by prefixing https://
        if not lower.startswith(("tg://", "http://", "https://")):
            spec = "https://" + spec
        parts = urlsplit(spec)
        # tg://proxy puts the kind in netloc; https://t.me/proxy in path.
        kind = (parts.path or parts.netloc).lstrip("/").lower()
        params = _query_params(parts.query)

        if kind == "proxy":
            return _parse_mtproto(params, spec)
        if kind == "socks":
            # tg://socks?server=...&port=...&user=...&pass=...
            server = params.get("server", "").strip()
            port = params.get("port", "").strip()
            if not server or not port:
                raise ValueError(
                    f"SOCKS proxy link must contain server and port, got: {spec!r}"
                )
            user = params.get("user", "")
            password = params.get("pass", "")
            uri = f"socks5://{server}:{port}"
            if user or password:
                from urllib.parse import quote

                uri = (
                    f"socks5://{quote(user)}:{quote(password)}"
                    f"@{server}:{port}"
                )
            return _parse_socks_uri(uri)

        raise ValueError(
            f"Unsupported tg:// proxy link kind {kind!r} in {spec!r}; "
            "expected proxy or socks"
        )

    return _parse_socks_uri(spec)


def proxy_description(spec: str) -> str:
    """Human-readable proxy summary for logs (never includes secret/password)."""
    kwargs = parse_proxy(spec)
    if not kwargs:
        return "direct connection"
    proxy = kwargs["proxy"]
    if isinstance(proxy, tuple):
        return f"mtproto {proxy[0]}:{proxy[1]}"
    conn = kwargs.get("connection")
    kind = proxy.get("proxy_type", "socks")
    user = proxy.get("username") or ""
    auth = " with auth" if user else ""
    conn_name = getattr(conn, "__name__", "default") if conn is not None else "default"
    return f"{kind} {proxy['addr']}:{proxy['port']}{auth} ({conn_name})"
