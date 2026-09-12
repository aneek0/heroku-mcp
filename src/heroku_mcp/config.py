"""Configuration management for Heroku MCP Server."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

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

    model_config = {"env_prefix": "HEROKU_MCP_"}


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
    # Accept a t.me topic link as her_chat_id: extract chat + topic.
    chat, topic = parse_chat_link(str(settings.her_chat_id))
    settings.her_chat_id = chat
    if not settings.her_topic_id:  # explicit her_topic_id wins over the link
        settings.her_topic_id = topic
    return settings


settings = load_settings()
