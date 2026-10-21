# 0002 — Answers are new rich messages, not edits

- **Status:** accepted
- **Date:** 2026-10-21

## Context

Until 2026-09 the userbot answered by editing the command message, and
`send_command` tracked edits of that one message until the text settled.

Since 2026-10 it answers through an **inline bot**: the reply is a *new*
message whose `Message.text` is empty; the content lives in
`Message.rich_message` (`RichMessage` with `PageBlock*` and `Text*` nodes).
The command message is often edited to an ack (`🪐`) and nothing else.

Observed on the live Logs topic (2026-10-21):

```
3247  .ping            (sent by us)
3248  "" + RichMessage  (via_bot_id=7785734702, ⚡️ 𝙿𝚒𝚗𝚐: 66.833 𝚖𝚜 …)
```

Edit-only tracking therefore returned the ack or an empty string.

A second, unrelated fact: Telethon 1.44 cannot deserialize the constructors
the account now receives (`0x48a30254 ReplyInlineMarkup`, `0xb1b8cc83` new
`User`); the update loop raises and stops delivering. 1.45 knows both.

## Decision

1. Watch **new messages** in the target chat/topic with `id >= sent_id`, from
   our own account (`out=True`), in the configured topic; keep watching edits
   as a secondary path (`.dlm` still edits in place).
2. Render every candidate through `heroku_mcp.rich.message_text()`:
   `text` when non-empty, otherwise the flattened `rich_message`.
3. Settle on the last text containing an alphanumeric character; an emoji ack
   keeps the window open until `wait` runs out.
4. Poll as a *fallback* only, every 3 s: `GetRepliesRequest` (topic polls) is
   flood-limited and a 0.5 s cadence earns ~12 s sleeps.
5. Require `telethon>=1.45`.

## Alternatives rejected

- **Regex over `rich_message` repr.** Fragile against new block types; the
  converter walks the TL tree instead.
- **Returning the raw rich JSON.** Tool consumers want text; JSON would leak a
  Telegram-specific schema into every client.
- **Polling only.** Events deliver the common case without API calls; polling
  stays as a safety net for reconnects.

## Consequences

- The parser contract is now `tests/fixtures/*.json` — raw `RichMessage` JSON
  captured from the live topic (`.ping`, `.help`) — replayed through real
  Telethon constructors in `tests/test_rich_fixtures.py`.
- Unknown block types degrade to their text/children or to `""`; they never
  raise.
- Latency is bound by the answer's arrival, not by `quiet`, for content-rich
  replies; a slow userbot still fits, because `wait` only caps a hung command.
- Telethon becomes a version-sensitive dependency: bump it when the account
  starts receiving constructors it does not know.
