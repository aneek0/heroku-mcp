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
python -m heroku_mcp.server                    # streamable HTTP on 127.0.0.1:6767
```

### stdio mode (for stdio-only MCP clients)

Some MCP clients only support stdio transport (e.g. jcode). Set
`HEROKU_MCP_STDIO=1` to speak MCP over stdin/stdout instead of HTTP:

```bash
HEROKU_MCP_STDIO=1 python -m heroku_mcp.server
```

Example jcode registration (`~/.jcode/mcp.json`):

```json
{
  "servers": {
    "heroku-mcp": {
      "command": "/path/to/heroku-mcp/.venv/bin/python",
      "args": ["-m", "heroku_mcp.server"],
      "env": { "HEROKU_MCP_STDIO": "1" },
      "shared": true
    }
  }
}
```

Only one instance can hold the Telegram session lock at a time: don't run
the HTTP and stdio servers simultaneously on the same session.

## Remote HTTP access

The default transport already is streamable HTTP (JSON-RPC on `POST /mcp`), so
"remote" mostly means binding to a reachable address and protecting the port.

**Recommended: keep loopback, tunnel in.** Nothing is exposed, no token needed:

```bash
python -m heroku_mcp.server                      # on the server, binds 127.0.0.1:6767
ssh -N -L 6767:127.0.0.1:6767 user@server        # on your machine
# MCP client connects to http://127.0.0.1:6767/mcp
```

**Bind to an interface directly** (LAN, container, or behind a reverse proxy).
A non-loopback `server_host` is refused without `auth_token`, so the server
cannot be reached by whoever finds the port:

```bash
export HEROKU_MCP_SERVER_HOST=10.0.0.5
export HEROKU_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
python -m heroku_mcp.server
```

Clients send the token as `Authorization: Bearer <token>` (or `X-Api-Key:
<token>`) to `http://10.0.0.5:6767/mcp`. Bad or missing token → `401`,
`GET /mcp` → `405`, `GET /healthz` → `200` (open, for uptime checks), an
unexpected `Host` header → `421`. Set `allowed_hosts` when a tunnel or reverse
proxy rewrites `Host` (e.g. `mcp.example.com`); env expects a JSON list:

```bash
export HEROKU_MCP_ALLOWED_HOSTS='["mcp.example.com"]'
```

Binding to `0.0.0.0` accepts the address of any interface of the machine, so
VPN/tunnel addresses (NetBird, Tailscale, WireGuard) work without extra
configuration — they are not necessarily the interface `ip route` prefers.

### stdio-only clients (jcode and others)

jcode reads `mcp.json` but only launches command-based servers: an entry with
`"type": "http"`/`"sse"` is skipped with a log line. Bridge the remote endpoint
with `mcp-remote`, which speaks stdio to the client and streamable HTTP to us.
jcode expands `${VAR}` in args from **its own** environment, not from the
server's `env` block, so the reliable shape is a small wrapper that reads the
token from a file:

```bash
# ~/.local/bin/heroku-mcp-remote
#!/usr/bin/env bash
export HEROKU_MCP_TOKEN="$(<~/.heroku-mcp-token)"
exec npx -y mcp-remote@latest "${HEROKU_MCP_URL:-http://100.71.96.174:6767/mcp}" \
    --header 'Authorization: Bearer ${HEROKU_MCP_TOKEN}' \
    --transport http-only --allow-http
```

```json
{
  "servers": {
    "heroku-mcp-remote": {
      "command": "/home/you/.local/bin/heroku-mcp-remote",
      "args": [], "env": {}, "shared": false
    }
  }
}
```

`--allow-http` is required because the URL is plain HTTP (put TLS in front with
a reverse proxy to drop it). The token lives in a 0600 file rather than
`mcp.json`. `mcp-remote` still expands `${HEROKU_MCP_TOKEN}` into its own node
`argv`, so the secret is readable from `/proc` by the same user — unavoidable
with this bridge. Set `shared: false`: a shared server is registered with the
long-lived `jcode serve` supervisor, which is not restarted when you only
restart your session and can keep a dead handle after the child exits.

TLS is not terminated here — use a reverse proxy, or an SSH/cloudflare tunnel
in front. `allow_unauthenticated: true` skips the token check on a remote bind
for a trusted network; the server logs a warning when it starts.

### `.dlm` module loading over a distance

`load_module` writes the source locally, serves it over HTTP and hands the URL
to the userbot. The URL must be reachable **by the userbot**, which is not the
same thing as reachable by your MCP client: it used to be hardcoded to
`127.0.0.1`, so loading only worked while MCP and the userbot shared a machine.
The store now follows `server_host` (override with `module_host`), which means a
remote bind publishes module sources — the server warns about it at startup.

* MCP and userbot on the same box: nothing to do (default).
* Reachable network address: `server_host`/`module_host` = that address, and the
  URL is derived from it.
* Anything else (the usual case across the internet — the userbot sits on
  another host): keep the store local and point the userbot at something it can
  reach, e.g. `module_host: 127.0.0.1` plus a port forward or tunnel:

```yaml
heroku_mcp:
  server_host: "10.0.0.5"     # MCP endpoint for remote clients
  module_host: "127.0.0.1"    # keep sources local
  module_base_url: "https://mcp.example.com/modules"   # -> /modules/<name>.py
```

`module_base_url` wins over the derived URL and must include a scheme. Without
a URL the userbot can fetch, `load_module` cannot work — the other tools
(commands, evaluate, restart, history) are unaffected.

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
| `server_host` | `HEROKU_MCP_SERVER_HOST` | `127.0.0.1` | HTTP bind address; non-loopback requires `auth_token` |
| `auth_token` | `HEROKU_MCP_AUTH_TOKEN` | — | Bearer token for every HTTP request (empty = disabled) |
| `allow_unauthenticated` | `HEROKU_MCP_ALLOW_UNAUTHENTICATED` | `false` | Permit a remote bind with no token (trusted network only) |
| `allowed_hosts` | `HEROKU_MCP_ALLOWED_HOSTS` | `[]` | Extra `Host`/Origin values (JSON list in env), e.g. a tunnel domain |
| `module_host` | `HEROKU_MCP_MODULE_HOST` | = `server_host` | Bind address of the `.dlm` module store |
| `module_base_url` | `HEROKU_MCP_MODULE_BASE_URL` | — | Public base URL the userbot fetches modules from |
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
- `get_history(limit)` — get recent messages from the target chat/topic (plain text)
- `get_history_json(limit)` — get recent messages from the target chat/topic (JSON)
- `switch_chat(chat, topic)` — switch the target chat/topic at runtime; accepts
  `me`, ids, `@username`, or a topic link like `https://t.me/c/3537976236/7`

### Forum topics

`her_chat_id` accepts a topic link — the chat and topic are extracted
automatically: `https://t.me/c/3537976236/7` → chat `3537976236`, topic `7`.
Commands are sent inside the topic (via `reply_to`), and `get_history` only
shows messages from that topic. Alternatively set `her_topic_id` separately
(explicit value wins over a link). `switch_chat` changes the target at
runtime without a restart.
