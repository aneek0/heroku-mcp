#!/usr/bin/env python3
"""Health-check MTProto proxies from a list: TCP connect + MTProto handshake.

Usage:
    .venv/bin/python check_proxies.py [url-or-file] [--timeout 10]

Exit code 0 if at least one proxy works. Prints a ranking.
"""
import argparse
import asyncio
import sys
import urllib.request
from pathlib import Path

from telethon import TelegramClient
from telethon.network.connection import tcpmtproxy

from heroku_mcp.config import settings
from heroku_mcp.proxy import parse_proxy

WORKER = asyncio.Semaphore(4)  # check up to 4 proxies concurrently


def load_lines(source: str) -> list[str]:
    p = Path(source)
    if p.exists():
        text = p.read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(source, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
    return [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


async def check_one(link: str, api_id: int, api_hash: str, timeout: float) -> tuple[str, str]:
    """Return (link, status). Status is 'OK' or a short error string."""
    async with WORKER:
        kwargs = parse_proxy(link)
        session = ":memory:"
        client = TelegramClient(session, api_id, api_hash, **kwargs)
        try:
            await asyncio.wait_for(client.connect(), timeout=timeout)
            return link, "OK"
        except Exception as e:
            msg = str(e).replace("\n", " ")[:90]
            return link, msg
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default="https://gitverse.ru/api/repos/Akres/Proxy/raw/branch/master/proxies.txt")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    links = load_lines(args.source)
    if not links:
        print("no proxies found in source")
        return 2

    print(f"checking {len(links)} proxies (timeout {args.timeout}s)…")
    tasks = [check_one(link, settings.api_id, settings.api_hash, args.timeout) for link in links]
    results = []
    for coro in asyncio.as_completed(tasks):
        link, status = await coro
        mark = "✅" if status == "OK" else "❌"
        print(f"  {mark} {link}  -> {status}")
        results.append((link, status))

    ok = [l for l, s in results if s == "OK"]
    print(f"\n{len(ok)}/{len(links)} working")
    for l in ok:
        print(f"  WORKING: {l}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
