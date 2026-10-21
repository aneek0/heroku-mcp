# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- Userbot answers are new **rich messages** (inline bot, 2026-10): the reply
  has an empty `text` and a `RichMessage` payload, so edit-tracking returned
  the `🪐` ack or nothing. `send_command` now watches new messages in the
  target topic, renders rich content to text (`heroku_mcp.rich`), and settles
  only on content-bearing text. `get_history`/`get_history_json` use the same
  renderer.
- Topic polling no longer hammers `GetRepliesRequest`: polls run every 3 s
  (a 0.5 s cadence earned ~12 s flood waits); events deliver the usual case.
- `load_module`/`unload_module` and the module store reject names containing
  `/` or `..`, so a module name cannot write or read outside `modules/`.
- `.help` parsing accepts the 2026-10 listing format (`🛑 Module: ( cmd | … )`).
- `load_module` returned the intermediate "🕔 Устанавливаю модуль…" stage: the
  2 s quiet window closed before the real result. It now settles on 8 s, so the
  load result or the userbot's error is what comes back.
- `load_module` fails fast when the derived URL is not HTTPS: the userbot's
  `.dlm` only downloads HTTPS URLs without credentials, so a loopback or
  plain-HTTP URL could never succeed.

### Changed
- `telethon>=1.45`: 1.44 cannot deserialize `ReplyInlineMarkup` / the new
  `User` constructor and its update loop dies with `TypeNotFoundError`.
- `send_command`/`send_command_tool`/`_execute` default `wait` raised to 30 s:
  the inline-bot answer can take 10-20 s.
- `get_history` fetches exactly `limit` topic messages instead of `limit * 3`.
- `requirements.txt` removed; `pyproject.toml` + `uv.lock` are authoritative.

### Added
- `GLOSSARY.md`, `docs/adr/0001-command-channel.md`,
  `docs/adr/0002-rich-answer-parsing.md`.
- Golden fixtures captured from the live topic (`tests/fixtures/*.json`) and
  module-name guard tests (94 offline tests total).
- `test_non_https_module_url_fails_fast` (95 offline tests total).

## [0.1.0] — 2026-09-16

First tagged release. Core: MCP server managing a Heroku Telegram userbot
over streamable HTTP (or stdio), module loading via `.dlm`, proxy support
with a failover pool.

### Added
- MCP tools: `load_module`, `unload_module`, `list_modules`, `evaluate`,
  `send_command_tool`, `restart_userbot` (`.restart -f` with boot
  verification), `get_history`/`get_history_json`, `switch_chat`.
- Remote HTTP access: configurable bind host, bearer-token auth
  (`Guard` ASGI wrapper, 401/405/healthz), DNS-rebinding protection
  (`allowed_hosts`), wildcard-bind interface discovery for VPN/tunnel
  addresses.
- stdio transport (`HEROKU_MCP_STDIO=1`) for stdio-only clients; documented
  `mcp-remote` bridging for jcode.
- Proxy support: MTProto `tg://proxy` links (dd/ee/padded secrets), SOCKS5/4,
  HTTP, `tg://socks` links; invalid spec fails fast. Proxy pool with
  `proxy_list_url` failover and `check_proxies.py` health-check CLI.
- Forum topic support: `her_chat_id` accepts `t.me/c/<chat>/<topic>` links,
  `her_topic_id` sets the topic explicitly.
- Session-lock protocol (fcntl) shared by the server and
  `generate_session.py`; lifespan survives Telegram startup failures with
  actionable errors.
- Module store (aiohttp) serving `modules/` for `.dlm` URLs; `module_host`
  / `module_base_url` derivation.
- Offline unit test suite (85 tests).
- MIT license.

### Changed
- `send_command` settles on the last message edit (quiet window) instead of
  returning intermediate "installing…" stages; `load_module` re-sends `.dlm`
  for an existing file (reload without unload).
- `get_client` uses `connect()` + auth check with retries instead of
  interactive `start()`.
- src/ layout for the `heroku_mcp` package.

### Fixed
- FastMCP lifespan now runs in streamable-HTTP mode (module store starts,
  entity cache is pre-synced for fresh sessions).
- Base64 proxy secrets containing `+`/`/` are preserved.
- Locked-session recovery path restored in `get_client`.
- Empty settle result maps to an explicit no-response message.
