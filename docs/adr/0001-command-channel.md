# 0001 — Manage the userbot over a Telegram command channel

- **Status:** accepted (in force since the first release)
- **Date:** 2026-10-21 (recorded; the decision predates this record)

## Context

The Heroku userbot must be controlled while it runs on a machine we do not
own: a netbook, or a Heroku dyno. It exposes no HTTP API, no RPC endpoint and
no callback interface. What it does expose is its own Telegram account: it
listens for `.`-prefixed commands in a configured chat and replies there.

An MCP server has to turn tool calls into that interaction and turn the reply
back into a tool result.

## Decision

The command channel is the transport:

1. The MCP server holds its own Telethon session for the *same account*.
2. A tool call becomes a text message in the **target chat** (optionally a
   forum **topic**).
3. The answer is read back from the same chat by `send_command`, which settles
   on the final text (see ADR 0002 for how answers are recognized).

No second control plane is added (no bot token, no privileged port, no
webhook). The module store's HTTP server exists only to hand module *bytes* to
the userbot.

## Alternatives rejected

- **Bot API control plane.** The userbot is a user account, not a bot; wiring a
  bot token would be a second identity with its own permissions and rate limits.
- **HTTP API bolted onto the userbot.** The userbot runs on a third-party host;
  opening a port there is a larger security surface than using the chat it
  already uses, and it does not survive a dyno redeploy.
- **Importing modules directly.** That would skip the userbot's loader
  (dependency loading, hot reload) and only work where the code runs.

## Consequences

- Every tool latency includes a Telegram round-trip; the channel is
  conversational, not RPC.
- Command output is **text**, so parsers (`_parse_help`) are coupled to the
  userbot's rendering and need fixtures when the rendering changes (2026-10:
  rich messages, see ADR 0002).
- Blocking destructive commands (`BLOCKED_COMMANDS`) is done in `server.py`,
  because the channel itself cannot authorize anything beyond "this account
  sent it".
- The MCP session and the userbot share one account: an outdated userbot
  library breaks its own update loop independently of this server.
