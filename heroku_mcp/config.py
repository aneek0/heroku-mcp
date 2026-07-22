"""Configuration management for Heroku MCP Server."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HerokuMcpSettings(BaseSettings):
    """Settings loaded from environment or config.yaml."""

    api_id: int = Field(default=0)
    api_hash: str = Field(default="")
    session_path: str = Field(default="sessions/heroku_mcp")
    server_port: int = Field(default=6767)
    her_chat_id: str = Field(default="me")
    modules_dir: str = Field(default="modules")
    her_topic_id: int = Field(default=0)

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
    return settings


settings = load_settings()
