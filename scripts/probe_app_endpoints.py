#!/usr/bin/env python3
"""Dump the raw read-only responses behind one sub-device, app endpoints included.

Written to answer "where does the app get a reading we never see", starting
with the low-battery alert the HTV245FRF raises while STA_BAT still reads 1.
Every call is a GET and nothing is written, so this is safe to run against a
live account at any time.

Three endpoints, for two different reasons. /app/device/event/list is the one
the integration never calls at all, and the likeliest home for an alert the
app shows and the status frame does not carry. appHome/get and getDeviceByHid
are endpoints the client does call, dumped here in full because its own
methods return the parsed subset it keeps, and a field it drops on the way
through is invisible from inside the integration.

The output is a verbatim account dump: device names, productKeys, iotIds, MACs
and the account's own home names all appear in it. That is the point of the
tool, and it is also why it must not be pasted into an issue as-is.

Usage:
    RAINPOINT_EMAIL=you@example.com RAINPOINT_PASSWORD=secret \
        python scripts/probe_app_endpoints.py --hid 182509 --mid 236547 --addr 1 --port 1

Credentials come from the environment (RAINPOINT_EMAIL, RAINPOINT_PASSWORD,
and optionally RAINPOINT_AREA_CODE), never from a flag or a committed file.
Output is raw JSON, uninterpreted: reading it is the point.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REQUEST_TIMEOUT_SECONDS = 60.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump app-only read endpoints for one sub-device.")
    parser.add_argument("--email", default=os.environ.get("RAINPOINT_EMAIL"), help="Account email (or RAINPOINT_EMAIL)")
    parser.add_argument("--area-code", default=os.environ.get("RAINPOINT_AREA_CODE") or "1", help="Login area code")
    parser.add_argument("--hid", type=int, required=True, help="Home id")
    parser.add_argument("--mid", type=int, required=True, help="Hub device id")
    parser.add_argument("--addr", type=int, required=True, help="Sub-device address on that hub")
    parser.add_argument("--port", type=int, default=1, help="1-based zone/port (default 1)")
    parser.add_argument("--size", type=int, default=50, help="Event page size (default 50)")
    return parser.parse_args(argv)


def _resolve_password() -> str | None:
    password = os.environ.get("RAINPOINT_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        return None
    return getpass.getpass("RainPoint account password: ") or None


async def _dump(client, session, label: str, path: str, params: dict) -> None:
    """GET one endpoint and print its body, pretty-printed when it is JSON.

    Borrows the client's base URL and auth headers rather than adding a method
    per endpoint to the shipped client: nothing in the integration reads these
    responses, and a probe is not a reason to grow its API surface.
    """
    print(f"\n===== {label}  {path}  {params}")
    async with session.get(f"{client._base_url}{path}", headers=client._auth_headers(), params=params) as resp:
        body = await resp.text()
    print(f"HTTP {resp.status}")
    try:
        print(json.dumps(json.loads(body), indent=1, ensure_ascii=False))
    except ValueError:
        print(body)


async def _run(args: argparse.Namespace, password: str) -> int:
    import aiohttp

    from custom_components.rainpoint.api.client import RainPointClient

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = RainPointClient(args.area_code, args.email, password, session)
        await client.ensure_logged_in()

        await _dump(
            client,
            session,
            "event/list",
            "/app/device/event/list",
            {"size": args.size, "mid": args.mid, "hid": args.hid, "addr": args.addr, "port": args.port},
        )
        await _dump(client, session, "appHome/get", "/app/member/appHome/get", {"hid": args.hid, "homeVersion": 0})
        await _dump(client, session, "getDeviceByHid", "/app/device/getDeviceByHid", {"hid": args.hid})
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(_REPO_ROOT))
    args = _parse_args(argv)
    if not args.email:
        print("Set RAINPOINT_EMAIL or pass --email.", file=sys.stderr)
        return 2
    password = _resolve_password()
    if not password:
        print("Set RAINPOINT_PASSWORD.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, password))


if __name__ == "__main__":
    raise SystemExit(main())
