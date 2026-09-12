# heroku-mcp

MCP server for managing a Heroku Telegram userbot.

## Features

- Load/unload/list Python modules on the userbot via `.dlm`/`.ulm`
- Evaluate Python expressions remotely (`.e`)
- Send arbitrary commands to the userbot
- Retrieve recent messages from the target chat for diagnostics

## Setup

```bash
pip install -e .
python generate_session.py --phone +<number>   # create Telegram session (honors proxy)
cp config.example.yaml config.yaml
# edit config.yaml with your api_id, api_hash, proxy
python -m heroku_mcp.server
```

If the MCP server reports "Telegram session is not authorized", (re)run
`generate_session.py` — it works through the proxy from config.yaml and
refuses to start if a server instance is holding the session lock (stop
that instance first). After authorizing, just retry the tool — no server
restart is needed.

## Configuration

Config via `config.yaml` or environment variables with `HEROKU_MCP_` prefix:

| Key | Env | Default | Description |
|-----|-----|---------|-------------|
| `api_id` | `HEROKU_MCP_API_ID` | — | Telegram API ID |
| `api_hash` | `HEROKU_MCP_API_HASH` | — | Telegram API hash |
| `session_path` | `HEROKU_MCP_SESSION_PATH` | `sessions/heroku_mcp` | Path to Telethon session file |
| `server_port` | `HEROKU_MCP_SERVER_PORT` | `6767` | MCP server port |
| `modules_dir` | `HEROKU_MCP_MODULES_DIR` | `modules` | Directory for module files |
| `her_chat_id` | `HEROKU_MCP_HER_CHAT_ID` | `me` | Target chat — `"me"` for Saved Messages, or a group/channel ID for a dedicated log group |
| `her_topic_id` | `HEROKU_MCP_HER_TOPIC_ID` | `0` | Forum topic ID within `her_chat_id` (0 = disabled) |
| `restart_boot_wait` | `HEROKU_MCP_RESTART_BOOT_WAIT` | `30` | Seconds to wait for the userbot to boot before verification after `.restart -f` |
| `proxy` | `HEROKU_MCP_PROXY` | — | Proxy for Telegram connection. Empty/absent = direct connection |

### Proxy

`proxy` accepts:

- MTProto proxy links: `tg://proxy?server=...&port=...&secret=...` (or `https://t.me/proxy?...`), including `dd`/`ee` padded secrets
- SOCKS5/4: `socks5://[user:pass@]host:port`, `socks4://host:port`
- HTTP: `http://host:port`
- SOCKS links: `tg://socks?server=...&port=...&user=...&pass=...` (or `t.me/socks?...`)

```yaml
heroku_mcp:
  proxy: "tg://proxy?server=203.0.113.1&port=1443&secret=dd00112233445566778899aabbccddee1"
```

The env var `HEROKU_MCP_PROXY` overrides the YAML value. An invalid `proxy` value
aborts startup instead of silently falling back to a direct connection.
`generate_session.py` uses the same proxy setting.

## MCP Tools

- `load_module(name, code)` — save and load a module (also updates without unloading)
- `unload_module(name)` — unload a module
- `list_modules()` — list loaded modules
- `evaluate(expr)` — evaluate Python expression
- `restart_userbot(verify)` — force-restart the userbot (`.restart -f`), optionally verify it comes back
- `send_command_tool(cmd)` — send a raw command
- `get_history(limit)` — get recent messages from the target chat (plain text)
- `get_history_json(limit)` — get recent messages from the target chat (JSON)
