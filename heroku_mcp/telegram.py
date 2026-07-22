"""Telethon wrapper for communicating with the Heroku userbot."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path
from typing import Optional, Union

from telethon import TelegramClient, events

from .config import settings

log = logging.getLogger(__name__)

_client: Optional[TelegramClient] = None
_resolved_entity = None
_session_lock_fd: Optional[int] = None


def _acquire_session_lock():
    """Acquire an exclusive lock on the session file to prevent concurrent writers."""
    lock_path = Path(settings.session_path).with_suffix(".session.lock")
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        global _session_lock_fd
        _session_lock_fd = fd
        log.info("Session lock acquired (pid=%d)", os.getpid())
        return fd
    except (OSError, IOError):
        log.warning("Could not acquire session lock — another process holds it")
        return None


def _release_session_lock():
    global _session_lock_fd
    if _session_lock_fd:
        try:
            fcntl.flock(_session_lock_fd.fileno(), fcntl.LOCK_UN)
            _session_lock_fd.close()
            _session_lock_fd = None
        except Exception:
            pass


def _kill_stale_session():
    """Release SQLite journal/WAL files to unblock a locked session."""
    base = Path(settings.session_path)
    if not base.suffix:
        base = base.with_suffix(".session")
    # Remove stale journal/WAL — safe, forces SQLite into rollback
    for suffix in ("-journal", "-wal", "-shm"):
        p = base.with_suffix(base.suffix + suffix)
        if p.exists():
            try:
                p.unlink()
                log.info("Removed stale file: %s", p)
            except OSError as e:
                log.warning("Could not remove %s: %s", p, e)


def _build_client() -> TelegramClient:
    session = Path(settings.session_path)
    if not session.suffix:
        session = session.with_suffix(".session")
    return TelegramClient(str(session), settings.api_id, settings.api_hash)


async def get_client() -> TelegramClient:
    global _client
    if _client is None:
        # Acquire exclusive lock to prevent concurrent session access
        lock = _acquire_session_lock()
        if lock is None:
            # Cannot get lock — another process is using the session
            raise RuntimeError("Cannot acquire session lock — another process holds it. Try again later.")
        _client = _build_client()
        try:
            await _client.start()
        except Exception as e:
            # If session is locked (another process holds it), kill stale once and retry
            if "locked" in str(e).lower():
                log.warning("Session locked, killing stale processes and retrying: %s", e)
                _kill_stale_session()
                await asyncio.sleep(1)
                _client = _build_client()
                await _client.start()
            else:
                _release_session_lock()
                raise
        log.info("Telethon client connected (user %s)", settings.api_id)
    return _client


async def shutdown() -> None:
    global _client, _resolved_entity
    if _client is not None:
        await _client.disconnect()
        _client = None
    _resolved_entity = None
    _release_session_lock()


async def _resolve_entity() -> Union[str, int, None]:
    """Resolve her_chat_id to a Telethon entity (cached)."""
    global _resolved_entity
    if _resolved_entity is not None:
        return _resolved_entity

    chat_id = settings.her_chat_id
    if chat_id == "me":
        _resolved_entity = None  # means "me" (Saved Messages)
        return None

    client = await get_client()
    try:
        entity = await client.get_entity(int(chat_id))
    except (ValueError, TypeError):
        entity = await client.get_entity(chat_id)
    _resolved_entity = entity
    return entity


def invalidate_entity():
    """Clear cached entity (call after switching chat)."""
    global _resolved_entity
    _resolved_entity = None


async def send_command(command: str, wait: float = 10.0) -> str:
    """Send a command to the configured chat and wait for Heroku's response.

    The Heroku userbot typically *edits* the sent message with the result.
    Uses ``MessageEdited`` event registered before sending for minimal latency.

    Args:
        command: The command string (e.g. '.help', '.e 1+1').
        wait: Max seconds to wait for a response.

    Returns:
        The response text, or "(no response)" on timeout.
    """
    client = await get_client()
    entity = await _resolve_entity()
    topic = settings.her_topic_id or None

    me = await client.get_me() if entity is None else None
    target = entity or me
    kwargs = {}
    if topic:
        kwargs["reply_to"] = topic

    target_id = getattr(target, "id", None)

    event_fut = asyncio.get_running_loop().create_future()
    sent_id = None

    @client.on(events.MessageEdited)
    async def _on_edit(event):
        if sent_id is not None and event.message.id != sent_id:
            return
        if target_id is not None and event.chat_id != target_id:
            return
        text = event.message.text or ""
        if text and text != command and not event_fut.done():
            event_fut.set_result(text)

    poll_entity = entity or me
    try:
        sent = await client.send_message(target, command, **kwargs)
        sent_id = sent.id
        log.info("Sent command: %s (msg_id=%d, chat=%s, topic=%s)",
                 command, sent_id, settings.her_chat_id, topic)

        deadline = asyncio.get_running_loop().time() + wait
        poll_interval = 0.25
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(asyncio.shield(event_fut), timeout=poll_interval)
                log.info("Event response: %d chars", len(event_fut.result()))
                return event_fut.result()
            except asyncio.TimeoutError:
                pass
            try:
                fresh = await client.get_messages(poll_entity, ids=sent_id)
                if fresh and fresh.text and fresh.text != command:
                    log.info("Poll response: %d chars", len(fresh.text))
                    return fresh.text
            except Exception:
                pass
    finally:
        client.remove_event_handler(_on_edit)

    return "(no response)"


async def ensure_watcher() -> None:
    """Pre-fetch target chat to force channel sync before first command."""
    client = await get_client()
    try:
        entity = await _resolve_entity()
        if entity is not None:
            await client.get_messages(entity, limit=1)
            log.info("Channel synced (chat=%s)", settings.her_chat_id)
    except Exception:
        pass
    log.info("Telegram client ready (chat=%s, topic=%s)",
             settings.her_chat_id, settings.her_topic_id)
