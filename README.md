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
python generate_session.py   # create Telegram session file
cp config.example.yaml config.yaml
# edit config.yaml with your api_id, api_hash
python -m heroku_mcp.server
```

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

## MCP Tools

- `load_module(name, code)` — save and load a module (also updates without unloading)
- `unload_module(name)` — unload a module
- `list_modules()` — list loaded modules
- `evaluate(expr)` — evaluate Python expression
- `send_command_tool(cmd)` — send a raw command
- `get_history(limit)` — get recent messages from the target chat (plain text)
- `get_history_json(limit)` — get recent messages from the target chat (JSON)
