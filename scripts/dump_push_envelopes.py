#!/usr/bin/env python3
"""Open one observer session and dump every push envelope it receives.

The push channel parses only the brace-prefixed section of a sub-device
envelope's pipe-delimited param string and discards the rest, so nothing in
the integration can say whether those other sections carry a hub identity.
The hub connectivity frame carries its mid in section 1's tail; whether a
sub-device envelope carries the same thing is the open question this script
exists to answer, because attributing a pushed reading to the right hub on a
multi-hub account depends on it.

Why a script and not a debug log line: the shipped client deliberately never
logs the contents of a handled push, and a temporary line that broke that
invariant would have to be published to reach a real install. This runs
against the cloud from a workstation instead, prints to stdout, and ships
nothing.

Usage:
    # List every hub on the account, with the mid each session binds to.
    RAINPOINT_EMAIL=you@example.com RAINPOINT_PASSWORD=secret \\
        python scripts/dump_push_envelopes.py

    # Bind an observer session to one hub and dump for 15 minutes.
    RAINPOINT_EMAIL=... RAINPOINT_PASSWORD=... \\
        python scripts/dump_push_envelopes.py --mid 236547 --seconds 900

    # Parser self-check, no network and no credentials needed.
    python scripts/dump_push_envelopes.py --self-check

Credentials come from the environment (RAINPOINT_EMAIL, RAINPOINT_PASSWORD,
and optionally RAINPOINT_AREA_CODE), never from a flag or a committed file.

WARNING: this opens a second observer session on the same account. A running
Home Assistant install holds one of its own, and a duplicate session may
displace it. The integration recovers on its own, but expect its push channel
to bounce while this runs. Sub-devices push only when they report, so a quiet
account can stay silent for a long time; drive a valve zone to force one.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_REQUEST_TIMEOUT_SECONDS = 60.0
_DEFAULT_WATCH_SECONDS = 900

# A hub connectivity frame captured 2026-07-31, used by --self-check so the
# section splitter is exercised without credentials or hardware.
_SAMPLE_HUB_FRAME = (
    '{"method":"thing.service.property.set","params":{"param":"#P260731181730000016822282236547|0|1785521850011|112882164350#"}}'
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump raw push envelopes from one observer session.",
    )
    parser.add_argument("--email", default=os.environ.get("RAINPOINT_EMAIL"))
    parser.add_argument(
        "--area-code",
        default=os.environ.get("RAINPOINT_AREA_CODE", "1"),
        help="Phone-dial-style area code the RainPoint login expects, e.g. 1 for US (or RAINPOINT_AREA_CODE)",
    )
    parser.add_argument("--mid", type=int, help="Hub mid to bind the observer session to. Omit to list hubs and exit.")
    parser.add_argument(
        "--seconds",
        type=int,
        default=_DEFAULT_WATCH_SECONDS,
        help=f"How long to stay connected and dump (default {_DEFAULT_WATCH_SECONDS}).",
    )
    parser.add_argument("--self-check", action="store_true", help="Run the parser self-check and exit.")
    return parser.parse_args(argv)


def _resolve_password() -> str:
    password = os.environ.get("RAINPOINT_PASSWORD")
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass("RainPoint password: ")
    return ""


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]


def _split_sections(payload: bytes) -> tuple[str | None, list[str]]:
    """Return (raw text, pipe-delimited sections of the inner param string).

    Mirrors how the shipped parser reaches the param string, then keeps every
    section instead of only the brace-prefixed one. A payload that is not a
    recognised envelope yields an empty section list, and the caller still has
    the raw text.
    """
    import json

    from custom_components.rainpoint.const import (
        MQTT_PUSH_METHOD,
        MQTT_PUSH_PARAMS_KEY,
        MQTT_PUSH_SECTION_DELIMITER,
    )

    text = payload.decode("utf-8", "replace")
    try:
        outer = json.loads(text)
    except ValueError:
        return text, []
    if not isinstance(outer, dict) or outer.get("method") != MQTT_PUSH_METHOD:
        return text, []
    params = outer.get("params")
    if not isinstance(params, dict):
        return text, []
    param_str = params.get(MQTT_PUSH_PARAMS_KEY)
    if param_str is None and len(params) == 1:
        param_str = next(iter(params.values()))
    if not isinstance(param_str, str):
        return text, []
    return text, param_str.split(MQTT_PUSH_SECTION_DELIMITER)


def _self_check() -> int:
    sys.path.insert(0, str(_REPO_ROOT))
    text, sections = _split_sections(_SAMPLE_HUB_FRAME.encode())
    assert text == _SAMPLE_HUB_FRAME, "raw text should round-trip unchanged"
    assert len(sections) == 4, f"expected 4 sections, got {len(sections)}"
    assert sections[0].endswith("236547"), "section 1 should carry the mid in its tail"
    assert sections[1] == "0", "section 2 should be the connected flag"
    # A bare non-envelope payload keeps its text and yields no sections.
    text, sections = _split_sections(b"not json at all")
    assert text == "not json at all"
    assert sections == []
    print("self-check passed")
    return 0


async def _collect_hubs(client) -> list[dict]:
    """Return every hub record across every home, with hid injected."""
    hubs: list[dict] = []
    for home in await client.list_homes():
        hid = home.get("hid")
        for hub in await client.get_devices_by_hid(hid):
            hub["hid"] = hid
            hubs.append(hub)
    return hubs


def _print_inventory(hubs: list[dict]) -> None:
    print(f"{'mid':>10}  {'hid':>8}  {'model':<18}  {'subs':>4}  name")
    for hub in hubs:
        print(
            f"{hub.get('mid', ''):>10}  {hub.get('hid', ''):>8}  "
            f"{hub.get('model') or '(none)':<18}  {len(hub.get('subDevices') or []):>4}  {hub.get('name') or ''}"
        )
    print("\nPick one with --mid. The observer session is account-scoped, so a session bound")
    print("to any hub receives the account's traffic; --mid only chooses the identity that")
    print("fetches the credentials and the mid the shipped client would have stamped.")


class _HassShim:
    """The two hass attributes RainPointMqttClient actually touches."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def async_add_executor_job(self, func, *args):
        return await self.loop.run_in_executor(None, func, *args)


def _build_dumping_client_class():
    from custom_components.rainpoint.api.mqtt import RainPointMqttClient

    class _DumpingMqttClient(RainPointMqttClient):
        """Print every envelope instead of routing it to a coordinator.

        _dispatch_push is the single place the shipped client decides what a
        payload is, so overriding it captures every frame family at once,
        including the ones the shipped parser rejects outright.
        """

        def _dispatch_push(self, topic: str, payload: bytes) -> None:
            text, sections = _split_sections(payload)
            print(f"\n=== {_stamp()}  len={len(payload)}  topic={topic}")
            print(f"raw: {text}")
            if sections:
                print(f"param split into {len(sections)} section(s):")
                for index, section in enumerate(sections, start=1):
                    kind = "JSON" if section.lstrip().startswith("{") else "text"
                    print(f"  [{index}] {kind} len={len(section)}: {section}")
            else:
                print("(not a recognised envelope, no param string to split)")
            sys.stdout.flush()

    return _DumpingMqttClient


async def _run_dump(args: argparse.Namespace, password: str) -> int:
    import aiohttp

    from custom_components.rainpoint.api.client import RainPointClient

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = RainPointClient(args.area_code, args.email, password, session)
        hubs = await _collect_hubs(client)

        if args.mid is None:
            _print_inventory(hubs)
            return 0

        hub = next((h for h in hubs if h.get("mid") == args.mid), None)
        if hub is None:
            print(f"No hub with mid {args.mid} on this account.", file=sys.stderr)
            return 2
        if not hub.get("deviceName") or not hub.get("productKey"):
            print(
                f"Hub {args.mid} carries no deviceName/productKey, so it cannot fetch observer\n"
                "credentials. That is what a Bluetooth wrapper record looks like; pick a real hub.",
                file=sys.stderr,
            )
            return 2

        dumping_class = _build_dumping_client_class()
        mqtt_client = dumping_class(
            _HassShim(asyncio.get_running_loop()),
            client,
            None,
            hub["deviceName"],
            hub["productKey"],
            hub_mid=hub["mid"],
            hub_hid=hub["hid"],
        )

        print(f"Binding an observer session to mid={hub['mid']} hid={hub['hid']} for {args.seconds}s.")
        print("A running Home Assistant push channel may be displaced while this holds a session.")
        print("Waiting for downlink. Drive a valve zone to force a sub-device report.\n")
        sys.stdout.flush()

        await mqtt_client.async_start()
        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            await mqtt_client.async_disconnect()
        print(f"\nDone. {mqtt_client.message_count} message(s) received.")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_check:
        return _self_check()
    password = _resolve_password()
    if not args.email or not password:
        print("RAINPOINT_EMAIL (env var or --email) and RAINPOINT_PASSWORD (env var) are required.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(_REPO_ROOT))
    return asyncio.run(_run_dump(args, password))


if __name__ == "__main__":
    sys.exit(main())
