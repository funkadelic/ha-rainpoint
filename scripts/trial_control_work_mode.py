#!/usr/bin/env python3
"""Careful hardware trial for controlWorkMode against a real sub-device.

The generic-control allowlist and every new hand-written valve model are
gated on proof that client.control_work_mode is the call that actually
commands the hardware, not just that the vendor app can. This script is the
maintainer tool for producing that proof: it sends one controlWorkMode
command to one explicitly named sub-device, then polls the device status so
the run-state read-back (STA_WKSTATE bit 0 on valve models) is captured
alongside the physical observation.

Usage:
    # List every hub and sub-device on the account, with the ids a command needs.
    RAINPOINT_EMAIL=you@example.com RAINPOINT_PASSWORD=secret \\
        python scripts/trial_control_work_mode.py

    # Dry run: show exactly what would be sent, send nothing.
    RAINPOINT_EMAIL=... RAINPOINT_PASSWORD=... \\
        python scripts/trial_control_work_mode.py --mid 346965 --addr 1 --port 1 --mode open --duration 120

    # The real trial. Watch the valve while this runs.
    RAINPOINT_EMAIL=... RAINPOINT_PASSWORD=... \\
        python scripts/trial_control_work_mode.py --mid 346965 --addr 1 --port 1 --mode open --duration 120 --execute

Credentials come from the environment (RAINPOINT_EMAIL, RAINPOINT_PASSWORD,
and optionally RAINPOINT_AREA_CODE), never from a flag or a committed file.
Nothing is sent without --execute, and every run prints the full raw
response and the polled status frames so the trial is reproducible evidence,
not an anecdote.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# A trial never needs a long run; anything longer than this is a config
# mistake, not an experiment.
_MAX_TRIAL_DURATION_SECONDS = 600
_DEFAULT_TRIAL_DURATION_SECONDS = 120

# How long, and how often, to poll status after the command. The HTV210B
# captures showed STA_WKSTATE flipping within one report cycle, so a few
# minutes of polling brackets both the start and (for short runs) the end.
_DEFAULT_WATCH_SECONDS = 180
_POLL_INTERVAL_SECONDS = 15

_REQUEST_TIMEOUT_SECONDS = 60.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one controlWorkMode command to one sub-device and watch the status read-back.",
    )
    parser.add_argument("--email", default=os.environ.get("RAINPOINT_EMAIL"), help="RainPoint account email (or RAINPOINT_EMAIL)")
    # No --password flag on purpose: argv leaks into shell history and
    # process listings. RAINPOINT_PASSWORD or the interactive prompt only.
    parser.add_argument(
        "--area-code",
        default=os.environ.get("RAINPOINT_AREA_CODE") or "1",
        help="Phone-dial-style area code the RainPoint login expects, e.g. 1 for US (or RAINPOINT_AREA_CODE)",
    )
    parser.add_argument("--mid", type=int, help="Hub device id carrying the target sub-device")
    parser.add_argument("--addr", type=int, help="Sub-device address on that hub")
    parser.add_argument("--port", type=int, help="1-based zone/port to command")
    parser.add_argument("--mode", choices=("open", "close"), help="Command to send")
    parser.add_argument(
        "--duration",
        type=int,
        default=_DEFAULT_TRIAL_DURATION_SECONDS,
        help=f"Run time in seconds for open, 1 to {_MAX_TRIAL_DURATION_SECONDS} (default {_DEFAULT_TRIAL_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--watch",
        type=int,
        default=_DEFAULT_WATCH_SECONDS,
        help=f"Seconds to keep polling status after the command (default {_DEFAULT_WATCH_SECONDS})",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send the command. Without this the command is printed and nothing is sent.",
    )
    return parser.parse_args(argv)


def _resolve_password() -> str | None:
    password = os.environ.get("RAINPOINT_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        return None
    return getpass.getpass("RainPoint account password: ") or None


def _decoder_for(model: str | None):
    """Return the hand-written decoder for a model, or None to print raw only.

    Imported lazily so listing mode works even if a decoder module ever grows
    an import this standalone script cannot satisfy.
    """
    from custom_components.rainpoint import api

    decoders = {
        "HTV210B": api.decode_htv210b,
        "HTV113FRF": api.decode_htv145frf,
        "HTV145FRF": api.decode_htv145frf,
        "HTV213FRF": api.decode_htv213frf_valve,
        "HTV245FRF": api.decode_htv213frf_valve,
        "HTV345FRF": api.decode_htv213frf_valve,
        "HTV405FRF": api.decode_htv213frf_valve,
    }
    return decoders.get(model or "")


async def _collect_hubs(client) -> list[dict]:
    """Return every hub record across every home, with hid injected."""
    hubs: list[dict] = []
    for home in await client.list_homes():
        hid = home.get("hid")
        for hub in await client.get_devices_by_hid(hid):
            hub["hid"] = hid
            hub.setdefault("_home_name", home.get("name"))
            hubs.append(hub)
    return hubs


def _print_inventory(hubs: list[dict]) -> None:
    print("Hubs and sub-devices on this account:\n")
    for hub in hubs:
        ident_ok = bool(hub.get("deviceName")) and bool(hub.get("productKey"))
        ident_note = "" if ident_ok else "  [no deviceName/productKey: controlWorkMode cannot address this record]"
        print(f"hub mid={hub.get('mid')} hid={hub.get('hid')} model={hub.get('model')!r} name={hub.get('name')!r}{ident_note}")
        for sub in hub.get("subDevices", []) or []:
            print(f"    addr={sub.get('addr')} model={sub.get('model')!r} name={sub.get('name')!r}")
    print("\nRe-run with --mid/--addr/--port/--mode (and --execute) to send a command.")


def _status_entry_for(status: dict, addr: int) -> dict | None:
    """Match the same 'D'-prefixed sid convention the coordinator resolves.

    Status entries carry ids like "D03", not the integer addr; the hub-level
    "state" entry and null-valued placeholder slots are skipped.
    """
    for entry in status.get("subDeviceStatus", []) or []:
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid.startswith("D"):
            continue
        try:
            if int(sid[1:]) == addr:
                return entry
        except ValueError:
            continue
    return None


def _print_status(label: str, entry: dict | None, decoder) -> None:
    if entry is None:
        print(f"{label}: no status entry for this addr")
        return
    raw = entry.get("value")
    print(f"{label}: time={entry.get('time')!r} value={raw!r}")
    if decoder and isinstance(raw, str):
        try:
            decoded = decoder(raw)
        except Exception as exc:
            print(f"{label}: decoder raised {exc!r}")
            return
        zones = decoded.get("zones")
        if zones:
            for zone_num in sorted(zones):
                print(f"{label}: zone {zone_num} -> {zones[zone_num]}")


async def _run_trial(args: argparse.Namespace, password: str) -> int:
    import aiohttp

    from custom_components.rainpoint.api.client import RainPointApiError, RainPointClient

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = RainPointClient(args.area_code, args.email, password, session)
        hubs = await _collect_hubs(client)

        if args.mid is None:
            _print_inventory(hubs)
            return 0

        if args.addr is None or args.port is None or args.mode is None:
            print("--mid needs --addr, --port, and --mode as well.", file=sys.stderr)
            return 2

        # Reject nonsense before it reaches the hardware: port 0 addresses no
        # zone, and a run time outside the trial window is a config mistake.
        if args.port < 1:
            print(f"--port is 1-based; got {args.port}.", file=sys.stderr)
            return 2
        if args.mode == "open" and not 1 <= args.duration <= _MAX_TRIAL_DURATION_SECONDS:
            print(
                f"--duration must be 1 to {_MAX_TRIAL_DURATION_SECONDS} seconds for open; got {args.duration}.",
                file=sys.stderr,
            )
            return 2

        hub = next((h for h in hubs if h.get("mid") == args.mid), None)
        if hub is None:
            print(f"No hub with mid={args.mid} on this account.", file=sys.stderr)
            return 2
        sub = next((s for s in hub.get("subDevices", []) or [] if s.get("addr") == args.addr), None)
        if sub is None:
            print(f"Hub mid={args.mid} has no sub-device at addr={args.addr}.", file=sys.stderr)
            return 2

        device_name = hub.get("deviceName") or ""
        product_key = hub.get("productKey") or ""
        if not device_name or not product_key:
            print(
                f"Hub mid={args.mid} carries no deviceName/productKey, so controlWorkMode cannot address it "
                "(this is the Bluetooth-only parent-record shape).",
                file=sys.stderr,
            )
            return 2

        mode = 1 if args.mode == "open" else 0
        duration = args.duration if mode == 1 else 0
        model = sub.get("model")
        decoder = _decoder_for(model)

        print(f"Target: hub mid={args.mid} ({hub.get('name')!r}), sub addr={args.addr} model={model!r} ({sub.get('name')!r})")
        print(f"Command: port={args.port} mode={mode} ({args.mode}) duration={duration}s")

        status = await client.get_device_status(args.mid)
        _print_status("before", _status_entry_for(status, args.addr), decoder)

        if not args.execute:
            print("\nDry run: nothing sent. Re-run with --execute to send this command.")
            return 0

        # A rejected command is a result, not a crash: the whole point of the
        # trial is to learn whether controlWorkMode drives this model, and a
        # rejection paired with an unchanged read-back is exactly that evidence.
        # So keep polling either way and report the outcome in the exit code.
        rejected = False
        try:
            response_state = await client.control_work_mode(
                mid=args.mid,
                addr=args.addr,
                device_name=device_name,
                product_key=product_key,
                port=args.port,
                mode=mode,
                duration=duration,
            )
        except RainPointApiError as exc:
            rejected = True
            print(f"controlWorkMode rejected: {exc}")
            print("Polling anyway so the read-back after the rejection is captured.")
        else:
            print(f"controlWorkMode accepted; response state={response_state!r}")
            if decoder and isinstance(response_state, str):
                try:
                    print(f"response decoded: {decoder(response_state).get('zones')}")
                except Exception as exc:
                    print(f"response state did not decode: {exc!r}")

        # One flaky poll must not discard the rest of the trial. The cloud
        # times a status request out often enough that an unguarded loop
        # loses the frames after it, which is the same way a trial stops
        # being evidence.
        elapsed = 0
        while elapsed < args.watch:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS
            try:
                status = await client.get_device_status(args.mid)
            except (TimeoutError, aiohttp.ClientError, RainPointApiError) as exc:
                print(f"after +{elapsed}s: status poll failed ({exc!r}); continuing")
                continue
            _print_status(f"after +{elapsed}s", _status_entry_for(status, args.addr), decoder)

        print("\nRecord the physical observation (did the valve run, and for how long) next to this output.")
        return 1 if rejected else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    password = _resolve_password()
    if not args.email or not password:
        print("RAINPOINT_EMAIL (env var or --email) and RAINPOINT_PASSWORD (env var) are required.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(_REPO_ROOT))
    return asyncio.run(_run_trial(args, password))


if __name__ == "__main__":
    sys.exit(main())
