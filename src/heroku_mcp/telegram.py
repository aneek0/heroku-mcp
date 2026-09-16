"""Telethon wrapper for communicating with the Heroku userbot."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar, Union

from telethon import TelegramClient, events
from telethon.errors import RPCError

from .config import settings
from .proxy import parse_proxy, parse_proxy_list, proxy_description

log = logging.getLogger(__name__)

# Errors that mean the proxy cluster went stale/broken mid-session: the
# request itself is valid, but the current MTProto connection must be
# re-established (a full reconnect picks a live proxy cluster). Telethon 1.44
# deserializes MTPROTO_CLUSTER_INVALID as a generic RPCError (no dedicated
# class in rpcerrorlist), so match by message string in addition to codes.
_TRANSIENT_MSG_RE = re.compile(
    r"MTPROTO_CLUSTER_INVALID|CLUSTER_INVALID|RECONNECT"
)

_client: Optional[TelegramClient] = None
_resolved_entity = None
_session_lock_fd: Optional[int] = None

# Single-flight: concurrent first calls (lifespan warm-up + a tool call)
# must not start two pool cycles at once — the second would burn the whole
# tool timeout or fail on the flock session lock.
_connect_lock = asyncio.Lock()

T = TypeVar("T")

# ---------------------------- proxy pool ----------------------------

_proxy_pool: list[str] = []          # candidate specs, order preserved
_pool_index: int = 0                 # next candidate to try (round-robin)
_current_proxy: Optional[str] = None  # spec of the connected client
_pool_loaded: bool = False           # list URL fetched at least once


def _load_proxy_pool() -> None:
    """Populate _proxy_pool from settings once (URL fetch, best-effort)."""
    global _proxy_pool, _pool_loaded, _pool_index
    if _pool_loaded:
        return
    _pool_loaded = True
    pool: list[str] = []
    url = settings.proxy_list_url.strip()
    if url:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "heroku-mcp/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", "replace")
            fetched = parse_proxy_list(text)
            log.info("Proxy list fetched from URL: %d candidates", len(fetched))
            pool.extend(fetched)
        except Exception as e:
            log.warning(
                "Could not fetch proxy list from %s: %s — falling back to 'proxy' setting",
                url, e,
            )
    if settings.proxy.strip():
        # The primary 'proxy' setting stays in the pool as a candidate too,
        # tried first (index 0 preserves current behavior when it works).
        if settings.proxy.strip() not in pool:
            pool.insert(0, settings.proxy.strip())
    _proxy_pool = pool
    _pool_index = 0
    if pool:
        log.info(
            "Proxy pool initialized: %d candidate(s), first: %s",
            len(pool), proxy_description(pool[0]),
        )
    else:
        log.info("Proxy pool empty — direct connection will be used")


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


def _build_client(proxy_spec: Optional[str] = None) -> TelegramClient:
    """Build a client; pool-aware when proxy_spec is None.

    No-arg call keeps old behavior (uses settings.proxy); passing a spec
    (pool candidate) builds a client pinned to that proxy.
    """
    if proxy_spec is None:
        proxy_spec = settings.proxy if settings.proxy.strip() else None
    session = Path(settings.session_path)
    if not session.suffix:
        session = session.with_suffix(".session")
    kwargs = parse_proxy(proxy_spec) if proxy_spec else {}
    if kwargs:
        log.info("Using proxy: %s", proxy_description(proxy_spec))
    client = TelegramClient(str(session), settings.api_id, settings.api_hash, **kwargs)
    global _current_proxy
    _current_proxy = proxy_spec
    return client


async def _connect_pool() -> TelegramClient:
    """Build a client trying pool candidates in order until one connects.

    No pool (or all candidates failed) → falls back to the old single-proxy
    path: build via settings.proxy and try 3 times (preserves the
    MTPROTO_CLUSTER_INVALID healing via plain reconnect).
    """
    _load_proxy_pool()
    last_err: Exception | None = None
    global _pool_index, _pool_loaded, _current_proxy
    n = len(_proxy_pool)
    if n:
        for offset in range(n):
            idx = (_pool_index + offset) % n
            spec = _proxy_pool[idx]
            client = _build_client(spec)
            try:
                await asyncio.wait_for(
                    client.connect(), timeout=settings.proxy_check_timeout
                )
                _pool_index = (idx + 1) % n
                log.info(
                    "Connected via pool proxy %d/%d: %s",
                    idx + 1, n, proxy_description(spec),
                )
                return client
            except Exception as e:
                last_err = e
                log.warning(
                    "Pool proxy %d/%d failed (%s): %s",
                    idx + 1, n, proxy_description(spec), e,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # All pool candidates failed — maybe the list is stale; refetch next
        # time _reset_client() runs.
        _pool_loaded = False
    # Legacy single-proxy path (also direct connection when proxy unset)
    client = _build_client()
    await _connect_with_retries(client)
    return client


async def _connect_with_retries(client: TelegramClient) -> None:
    """Connect with up to 3 attempts (unstable proxies drop handshakes)."""
    for attempt in range(1, 4):
        try:
            # Telethon 1.44: connect() returns None on success and raises
            # on failure; sender.connect returns False only when already
            # connected (which is also fine).
            await client.connect()
            return
        except Exception as e:
            log.warning("Telethon connect failed (attempt %d/3): %s", attempt, e)
        if attempt < 3:
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(2)
    raise ConnectionError(
        "Could not connect to Telegram via "
        f"{proxy_description(settings.proxy) if settings.proxy.strip() else 'direct connection'}. "
        "Check the proxy/network."
    )


async def get_client() -> TelegramClient:
    global _client
    async with _connect_lock:
        if _client is None:
            return await _connect_locked()
        return _client


async def _connect_locked() -> TelegramClient:
    """Connect under _connect_lock (caller must hold it)."""
    global _client
    if _client is not None:
        return _client
    # Acquire exclusive lock to prevent concurrent session access
    lock = _acquire_session_lock()
    if lock is None:
        # Cannot get lock — another process is using the session
        raise RuntimeError("Cannot acquire session lock — another process holds it. Try again later.")
    try:
        _client = await _connect_pool()
        try:
            if not await _client.is_user_authorized():
                await _client.disconnect()
                _client = None
                _release_session_lock()
                raise RuntimeError(
                    "Telegram session is not authorized. Run "
                    "'python generate_session.py --phone +<number>' to log in"
                    " (it honors the proxy setting), then retry this tool -"
                    " no server restart is needed."
                )
        except Exception as e:
            if "locked" not in str(e).lower():
                raise
            # SQLite session locked by a stale writer — clean up once, rebuild, retry
            log.warning("Session locked, killing stale processes and retrying: %s", e)
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
            _kill_stale_session()
            await asyncio.sleep(1)
            _client = await _connect_pool()
            if not await _client.is_user_authorized():
                await _client.disconnect()
                _client = None
                _release_session_lock()
                raise RuntimeError(
                    "Telegram session is not authorized. Run "
                    "'python generate_session.py --phone +<number>' to log in"
                    " (it honors the proxy setting), then retry this tool -"
                    " no server restart is needed."
                )
    except Exception:
        if _client is not None:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        _release_session_lock()
        raise
    log.info("Telethon client connected (api_id=%s, dc=%s)",
             settings.api_id, _client.session.dc_id)
    return _client


async def shutdown() -> None:
    global _client, _resolved_entity
    if _client is not None:
        await _client.disconnect()
        _client = None
    _resolved_entity = None
    _release_session_lock()


def _is_transient(exc: BaseException) -> bool:
    """True if the error means the MTProto connection went stale mid-session.

    Two families:
    * ConnectionError / TimeoutError / ConnectionError subclasses from Telethon
      (transport dropped, proxy flaked).
    * Telegram RPC errors that indicate the server routing our connection is
      now invalid — e.g. the MTProto proxy cluster rotated and our DC binding
      ("cluster") expired. Telethon 1.44 ships no dedicated class for
      MTPROTO_CLUSTER_INVALID, so it arrives as a generic RPCError and we
      match the message string.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, RPCError):
        return bool(_TRANSIENT_MSG_RE.search(str(exc)))
    # Telethon wraps some transport failures in generic Exceptions
    msg = str(exc)
    return bool(_TRANSIENT_MSG_RE.search(msg))


async def _reset_client() -> None:
    """Full reconnect: drop the client and rebuild a fresh connection.

    A reconnect is what heals a stale proxy cluster: the new handshake may
    land on a different backend route. Keeps the session lock held (the lock
    protects the session file, not a single connection) — Telethon reopens
    the SQLite file on connect(). With a pool configured, the next candidate
    (round-robin) is tried first; the failed proxy is retried last on the next
    cycle. When the whole pool is stale, _connect_pool() refetches the list.
    """
    global _client, _resolved_entity
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception as e:
            log.warning("Error during disconnect before reconnect: %s", e)
    _client = await _connect_pool()
    # Entity access hashes can differ across DCs; force re-resolution.
    _resolved_entity = None
    log.info("Telethon client reconnected after transient failure")


async def _call_with_recovery(coro_factory: Callable[[], Awaitable[T]],
                              attempts: int = 3) -> T:
    """Run a Telegram call, reconnecting once on transient errors.

    coro_factory must re-create the coroutine on every attempt (a used
    coroutine object cannot be awaited twice).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            if not _is_transient(e) or attempt == attempts:
                raise
            last_exc = e
            log.warning(
                "Transient Telegram error (attempt %d/%d), reconnecting: %s",
                attempt, attempts, e,
            )
            await _reset_client()
    raise last_exc  # unreachable, keeps type-checkers happy


async def _resolve_entity() -> Union[str, int, None]:
    """Resolve her_chat_id to a Telethon entity (cached).

    A freshly generated session has an empty entity cache, so resolving a
    raw user ID fails with "Could not find the input entity". Fetching the
    dialogs once warms the cache (access hashes) and makes the retry work.
    """
    global _resolved_entity
    if _resolved_entity is not None:
        return _resolved_entity

    chat_id = settings.her_chat_id
    if chat_id == "me":
        _resolved_entity = None  # means "me" (Saved Messages)
        return None

    client = await get_client()

    async def _try_resolve() -> object:
        try:
            return await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            return await client.get_entity(chat_id)

    try:
        entity = await _try_resolve()
    except ValueError:
        # Fresh session: warm the entity cache from dialogs, retry once.
        log.info("Entity %s not cached, fetching dialogs to warm cache", chat_id)
        await client.get_dialogs(limit=100)
        entity = await _try_resolve()
    _resolved_entity = entity
    return entity


def invalidate_entity():
    """Clear cached entity (call after switching chat)."""
    global _resolved_entity
    _resolved_entity = None


async def set_target_chat(chat: Union[str, int], topic: int = 0) -> str:
    """Switch the chat (and optional forum topic) commands are sent to.

    Args:
        chat: chat id, @username, "me" (Saved Messages), or a t.me link
            (t.me/c/<id>/<topic> / t.me/<username>/<topic>).
        topic: forum topic id; 0 = whole chat. A topic inside a passed link
            overrides this argument.

    Returns a human-readable confirmation (no secrets).
    """
    from .config import parse_chat_link

    chat, link_topic = parse_chat_link(str(chat))
    if link_topic:
        topic = link_topic  # topic inside the link wins over the argument
    settings.her_chat_id = chat
    settings.her_topic_id = int(topic)
    invalidate_entity()
    # Verify resolvability right away so the caller gets an actionable error.
    entity = await _resolve_entity()
    if entity is None:
        where = "Saved Messages"
    else:
        where = getattr(entity, "title", None) or str(chat)
    topic_str = f", topic {settings.her_topic_id}" if settings.her_topic_id else ""
    log.info("Target chat switched: %s%s", where, topic_str)
    return f"Commands will now be sent to: {where}{topic_str}"


async def send_command(command: str, wait: float = 15.0, quiet: float = 2.0) -> str:
    """Send a command to the configured chat and return the *final* response.

    The Heroku userbot typically *edits* the sent message with the result, and
    some commands edit the same message several times (e.g. ``.dlm`` prints
    "installing..." first and the load result a second later). To avoid
    returning an intermediate stage, this tracks edits until no new edit
    arrives for ``quiet`` seconds and returns the last text.

    Typical latency is (command runtime) + ``quiet``; ``wait`` is only an
    emergency ceiling for a hung command, not the expected duration.

    Args:
        command: The command string (e.g. '.help', '.e 1+1').
        wait: Max seconds to wait overall.
        quiet: Consider the message settled after this many seconds without a new edit.

    Returns:
        The final response text, or "" on timeout (``_execute`` maps it to a
        human-readable "(no response)").
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
    loop = asyncio.get_running_loop()

    edit_fut: asyncio.Future = loop.create_future()
    sent_id = None
    last_text = ""
    last_edit_at: float | None = None
    seen_texts: set[str] = set()

    def _accept(text: str) -> None:
        nonlocal last_text
        # Monotonic: every distinct text is a new stage; a poll returning an
        # older stage text must not regress the state machine.
        if text and text != command and text != last_text and text not in seen_texts and not edit_fut.done():
            seen_texts.add(text)
            last_text = text
            edit_fut.set_result(text)

    @client.on(events.MessageEdited)
    async def _on_edit(event):
        if sent_id is not None and event.message.id != sent_id:
            return
        if target_id is not None and event.chat_id != target_id:
            return
        _accept(event.message.text or "")

    poll_entity = entity or me
    _poll_reconnected = False
    try:
        sent = await client.send_message(target, command, **kwargs)
        sent_id = sent.id
        log.info("Sent command: %s (msg_id=%d, chat=%s, topic=%s)",
                 command, sent_id, settings.her_chat_id, topic)

        deadline = loop.time() + wait
        poll_interval = 0.25
        while loop.time() < deadline:
            try:
                text = await asyncio.wait_for(asyncio.shield(edit_fut), timeout=poll_interval)
                log.info("Edit received: %d chars", len(text))
                edit_fut = loop.create_future()
                last_edit_at = loop.time()
                continue
            except asyncio.TimeoutError:
                pass
            try:
                fresh = await client.get_messages(poll_entity, ids=sent_id)
                if fresh and fresh.text:
                    _accept(fresh.text)
            except Exception as poll_err:
                if _is_transient(poll_err) and not _poll_reconnected:
                    _poll_reconnected = True
                    log.warning("Poll hit transient error, reconnecting: %s", poll_err)
                    await _reset_client()
                    # _reset_client() replaced the module-level client; refresh
                    # the local reference and re-register the edit handler on
                    # the new connection (the old one is dead).
                    client.remove_event_handler(_on_edit)
                    client = _client
                    client.add_event_handler(_on_edit, events.MessageEdited)
                # Non-transient or already reconnected: keep polling locally,
                # the event handler may still deliver the response.

            # No new edit within `quiet` seconds — the message has settled.
            if last_text and last_edit_at is not None and loop.time() - last_edit_at >= quiet:
                break
    finally:
        client.remove_event_handler(_on_edit)

    return last_text


async def send_command_final(command: str, wait: float = 30.0, quiet: float = 5.0) -> str:
    """Send a command and return the *final* text once successive edits settle.

    Thin alias over :func:`send_command`, which now always settles: kept for
    readability at call sites (e.g. ``.restart -f``) that need generous
    ``wait``/``quiet`` budgets spread over a long boot.

    Args:
        command: The command string to send.
        wait: Overall deadline in seconds.
        quiet: Consider the message settled after this many seconds without a new edit.
    """
    return await send_command(command, wait=wait, quiet=quiet)


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
