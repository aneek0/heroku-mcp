# GLOSSARY

Canonical vocabulary for `heroku-mcp`. Terms are listed once; code and docs use
them exactly as defined here.

## Actors

**Userbot** — the Python Telegram client ("Heroku") running elsewhere (netbook
or Heroku), logged in as the user's own account. It is the *executor*: it runs
modules and answers commands. The MCP server never executes module code.

**MCP server** — this project. Speaks MCP to an IDE/agent, speaks Telegram to
the userbot. Holds its own Telethon session (`sessions/heroku_mcp.session`).

**Inline bot** — the bot (`via_bot_id`) the userbot uses to post **rich
answers**. It is not addressed directly by the MCP server.

## Channel

**Target chat** — where commands are sent (`her_chat_id`). May be Saved
Messages (`"me"`), a group, or a forum supergroup.

**Topic** — a forum thread inside the target chat (`her_topic_id`, taken from a
`t.me/c/<chat>/<topic>` link). Commands and answers stay inside it.

**Command channel** — the whole mechanism: a text command (`.help`, `.dlm`,
`.e …`) posted into the target chat, the userbot acting on it, an **answer**
appearing in the same topic. There is no RPC, no callback, no bot API token.

**Command echo** — the userbot re-posting the command text as its own message
before processing. Never an answer; the settle logic drops it.

**Ack** — a progress edit on the command/echo message (`🪐`). Carries no
answer; it must not close the settle window. See `_has_content`.

**Answer** — the userbot's reply. Since 2026-10 it is a **new** message, not an
edit of the command:

**Rich message** — a Telegram message whose `Message.text` is empty and whose
content is a `RichMessage` tree (`PageBlock*` / `Text*`). Rendered to text by
`heroku_mcp.rich`. Detected by `rich_message is not None`.

**Settle** — `send_command`'s termination rule: the last content-bearing text
wins, and the call returns once no newer answer arrives for `quiet` seconds.
`wait` is only the ceiling for a hung command.

**Message id watermark** — `sent_id`; answers must have a larger id. Polling
uses it as `min_id`.

## Modules

**Module** — a `.py` file in `modules/` on the MCP host, plus the loaded
instance inside the userbot. Name pattern `^[A-Za-z0-9_][A-Za-z0-9_.-]*$`;
names never contain `/`.

**Module store** — aiohttp server on `server_port + 1` serving `modules/<name>.py`
raw, so the userbot can fetch it. Unauthenticated by design; warns when bound
off-box.

**`.dlm`** — the userbot command that downloads a module from a URL and loads
it. `.ulm` unloads.

## Transport

**Proxy pool** — ordered proxy specs (`settings.proxy` first, then
`proxy_list_url`); each reconnect tries the next candidate.

**Session lock** — flock on `<session>.lock`. One process per session file;
`generate_session.py` and the server share the protocol.
