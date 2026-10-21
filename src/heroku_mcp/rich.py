"""Plain-text rendering for Telegram rich messages.

Since 2026-10 the Heroku userbot answers through an inline bot with a
*rich message*: ``Message.text`` is empty and the content lives in
``Message.rich_message`` (``RichMessage`` / ``PageBlock*`` / ``Text*``).
Every caller that wants the answer as text must go through
:func:`message_text`.
"""

from __future__ import annotations

from typing import Any, Iterable



def _text(node: Any) -> str:
    """Flatten one ``Text*`` node to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    name = type(node).__name__
    if name == "TextEmpty" or name == "TextImage":
        return ""
    if name == "TextConcat":
        return "".join(_text(t) for t in (node.texts or []))
    inner = getattr(node, "text", None)
    if inner is not None:
        return inner if isinstance(inner, str) else _text(inner)
    return ""


def _blocks(blocks: Iterable[Any] | None) -> str:
    out = [_block(b) for b in (blocks or [])]
    return "\n".join(part for part in out if part)


def _list_items(block: Any, ordered: bool) -> str:
    lines = []
    for i, item in enumerate(block.items or [], 1):
        body = _block(item).strip()
        if body:
            lines.append(f"{i}. {body}" if ordered else f"• {body}")
    return "\n".join(lines)


def _block(block: Any) -> str:
    """Render one ``PageBlock*`` node; unknown blocks degrade to their text."""
    if block is None:
        return ""
    name = type(block).__name__
    if name == "PageBlockDivider":
        return "---"
    if name in ("PageBlockList", "PageBlockOrderedList"):
        return _list_items(block, ordered=name == "PageBlockOrderedList")
    if name == "PageBlockTable":
        rows = []
        for row in block.rows or []:
            cells = [_text(getattr(cell, "text", cell)) for cell in (row.cells or [])]
            rows.append(" | ".join(cells))
        return "\n".join(rows)

    parts: list[str] = []
    title = getattr(block, "title", None)
    if title is not None:
        parts.append(_text(title))
    text = getattr(block, "text", None)
    if text is not None:
        parts.append(_text(text))
    caption = getattr(block, "caption", None)
    if caption is not None:
        parts.append(_text(caption))
    inner = getattr(block, "blocks", None)
    if inner:
        parts.append(_blocks(inner))
    return "\n".join(part for part in parts if part)


def rich_to_text(rich: Any) -> str:
    """Flatten a ``RichMessage`` to plain text."""
    if rich is None:
        return ""
    return _blocks(getattr(rich, "blocks", None))


def message_text(msg: Any) -> str:
    """Text of a reply, whether it is a plain message or a rich one."""
    text = getattr(msg, "text", "") or ""
    if text:
        return text
    return rich_to_text(getattr(msg, "rich_message", None))
