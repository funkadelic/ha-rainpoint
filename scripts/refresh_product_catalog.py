#!/usr/bin/env python3
"""Regenerate the committed, trimmed RainPoint product catalog.

The integration ships a committed snapshot of the vendor's product-model
catalog at custom_components/rainpoint/api/data/product_catalog.json and
never fetches it from the vendor at runtime. This script is the maintainer
tool that keeps that snapshot up to date: it authenticates with a
maintainer's own RainPoint credentials, pulls the live catalog, trims it
down to the fields the integration actually uses, and either writes the
result to the committed file or diffs it against what is already committed.

Usage:
    RAINPOINT_EMAIL=you@example.com RAINPOINT_PASSWORD=secret \\
        python scripts/refresh_product_catalog.py

    RAINPOINT_EMAIL=you@example.com RAINPOINT_PASSWORD=secret \\
        python scripts/refresh_product_catalog.py --check

Credentials are always read from the environment (RAINPOINT_EMAIL,
RAINPOINT_PASSWORD, and optionally RAINPOINT_AREA_CODE) or passed as CLI
arguments. Never hardcode credentials in this file or in a committed config.

Write mode (the default) overwrites the committed catalog file with a fresh,
trimmed pull. --check mode never writes; it prints a drift summary and exits
nonzero when the committed file no longer matches a live pull, so it is safe
to run on a schedule in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG_PATH = _REPO_ROOT / "custom_components" / "rainpoint" / "api" / "data" / "product_catalog.json"

# Only these model-name prefixes are kept -- devices report RainPoint model
# strings, so any other vendor catalog entry is never looked up and would
# only bloat the committed file.
_MODEL_PREFIXES = ("HTV", "HCS", "HWS", "HWG", "HIC")

# Per dp entry, keep only the fields the loader/enrichment needs. Drop
# UI/provisioning metadata the vendor catalog also carries.
_KEPT_DP_FIELDS = ("dpCode", "identity", "dpPort", "dpDataType", "portNumber")


def trim_catalog(raw: list[dict]) -> dict:
    """Trim a raw vendor productModel catalog to the committed snapshot shape.

    raw is the list returned by RainPointClient.get_product_catalog(): one
    entry per vendor model, each carrying a "model" name and a "dp" list of
    per-datapoint metadata dicts. Returns a flat object keyed by model
    string, where RainPoint-prefixed models keep only their dp entries'
    dpCode/identity/dpPort/dpDataType/portNumber fields and every other
    model is dropped. Pure function: no I/O, no network.
    """
    trimmed: dict[str, list[dict]] = {}
    for entry in raw:
        model = entry.get("model")
        if not model or not str(model).startswith(_MODEL_PREFIXES):
            continue
        dp_entries = entry.get("dp") or []
        # Sort by dpCode so re-running against an unchanged vendor catalog is
        # deterministic, even if the vendor API does not guarantee a stable
        # dp array order across calls. Entries missing dpCode sort last.
        trimmed[model] = sorted(
            ({field: dp.get(field) for field in _KEPT_DP_FIELDS} for dp in dp_entries),
            key=lambda d: (d.get("dpCode") is None, d.get("dpCode")),
        )
    return trimmed


def _write_catalog(trimmed: dict, path: Path) -> None:
    """Write the trimmed catalog with stable key ordering and a trailing newline.

    Writes to a temp file in the same directory first and atomically replaces
    the destination, so an interrupted write (Ctrl-C, disk full, OOM kill)
    cannot leave the previously-committed file truncated or corrupted.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(trimmed, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def _load_committed_catalog(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _print_drift(committed: dict, fresh: dict) -> bool:
    """Print a human-readable drift summary. Returns True if any drift was found."""
    committed_models = set(committed)
    fresh_models = set(fresh)

    added = sorted(fresh_models - committed_models)
    removed = sorted(committed_models - fresh_models)
    changed = sorted(model for model in committed_models & fresh_models if committed[model] != fresh[model])

    if not added and not removed and not changed:
        print("No drift: the committed catalog matches a fresh pull.")
        return False

    if added:
        print(f"Models added upstream ({len(added)}): {', '.join(added)}")
    if removed:
        print(f"Models removed upstream ({len(removed)}): {', '.join(removed)}")
    if changed:
        print(f"Models with changed dp entries ({len(changed)}): {', '.join(changed)}")
    return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate the committed RainPoint product catalog from a live vendor pull.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff the committed catalog against a fresh live pull and exit nonzero on drift, without writing.",
    )
    parser.add_argument("--email", default=os.environ.get("RAINPOINT_EMAIL"), help="RainPoint account email (or RAINPOINT_EMAIL)")
    parser.add_argument(
        "--password", default=os.environ.get("RAINPOINT_PASSWORD"), help="RainPoint account password (or RAINPOINT_PASSWORD)"
    )
    parser.add_argument(
        "--area-code",
        default=os.environ.get("RAINPOINT_AREA_CODE", "1"),
        help="Phone-dial-style area code the RainPoint login expects, e.g. 1 for US (or RAINPOINT_AREA_CODE)",
    )
    return parser.parse_args(argv)


async def _fetch_trimmed_catalog(email: str, password: str, area_code: str) -> dict:
    import aiohttp

    from custom_components.rainpoint.api.client import RainPointClient

    async with aiohttp.ClientSession() as session:
        client = RainPointClient(area_code, email, password, session)
        raw = await client.get_product_catalog()
    return trim_catalog(raw)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not args.email or not args.password:
        print("RAINPOINT_EMAIL and RAINPOINT_PASSWORD (env vars or --email/--password) are required.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(_REPO_ROOT))
    trimmed = asyncio.run(_fetch_trimmed_catalog(args.email, args.password, args.area_code))

    if args.check:
        committed = _load_committed_catalog(_CATALOG_PATH)
        drifted = _print_drift(committed, trimmed)
        return 1 if drifted else 0

    committed = _load_committed_catalog(_CATALOG_PATH)
    if not trimmed:
        print(
            "Refusing to write an empty catalog (live pull produced 0 kept models); check the vendor response before retrying.",
            file=sys.stderr,
        )
        return 1
    if committed and len(trimmed) < len(committed) // 2:
        print(
            f"Refusing to write: model count dropped from {len(committed)} to {len(trimmed)}; "
            "investigate before overwriting the committed catalog.",
            file=sys.stderr,
        )
        return 1

    _write_catalog(trimmed, _CATALOG_PATH)
    print(f"Wrote {len(trimmed)} models to {_CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
