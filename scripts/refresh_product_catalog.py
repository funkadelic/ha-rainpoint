#!/usr/bin/env python3
"""Regenerate the committed, trimmed RainPoint product catalog.

The integration ships a committed snapshot of RainPoint's product-model
catalog at custom_components/rainpoint/api/data/product_catalog.json and
never fetches it from RainPoint at runtime. This script is the maintainer
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
import getpass
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG_PATH = _REPO_ROOT / "custom_components" / "rainpoint" / "api" / "data" / "product_catalog.json"

# Only these model-name prefixes are kept -- devices report RainPoint model
# strings, so any other RainPoint catalog entry is never looked up and would
# only bloat the committed file.
_MODEL_PREFIXES = ("HTV", "HCS", "HWS", "HWG", "HIC")

# Per dp entry, keep only the fields the loader/enrichment needs. Drop
# UI/provisioning metadata the RainPoint catalog also carries.
#
# dpLen is RainPoint's own byte count and is what the enrichment compares a
# decoded field's width against; dpDataType ("U8", "S16", ...) is kept for its
# signedness letter. portNumber is deliberately absent: it is a per-model
# property, not a per-dp one, and is written once on the variant record.
_KEPT_DP_FIELDS = ("dpCode", "identity", "dpPort", "dpDataType", "dpLen")

# Identity namespaces kept in the committed snapshot.
#
# STA_ entries are the status datapoints the generic decoder actually sees in a
# payload. CTL_ entries are the control datapoints the generic-control
# allowlist is built from. The RainPoint catalog also carries P_/C_/S_/ATTR_/MAX_/
# RD_ provisioning, config, and UI metadata that no code path in this
# integration reads, so it is dropped rather than shipped to every user.
_KEPT_IDENTITY_PREFIXES = ("STA_", "CTL_")

# Per-variant provenance flags copied straight from the RainPoint entry. No code
# path decodes with them; they exist so a maintainer triaging an unfamiliar
# model can tell what kind of catalog record it is without re-fetching the raw
# RainPoint response.
#
# hasDistribution marks a record the app can actually pair, which is the
# closest thing the catalog has to "this product exists." HCS003FRF is the
# worked example: false here, absent from RainPoint's manual index and from the
# app's add-device list, yet it carried a hand-written decoder claiming
# moisture support for a device with no moisture datapoint.
#
# Read them as triage signals, not verdicts. false does not mean discontinued:
# accessory and sub-device records (accessoryFlag true) are false too, and so
# are several models this integration genuinely supports.
_KEPT_PROVENANCE_FIELDS = ("hasDistribution", "isMainDevice", "accessoryFlag")

# Total seconds allowed for login plus the catalog fetch. Well above the
# fraction of a second the endpoint normally takes, and far below aiohttp's
# five-minute default, which is long enough that a stalled run looks wedged.
_DEFAULT_TIMEOUT_SECONDS = 90.0

# Bucket key for RainPoint entries carrying no modelCode. Duplicated from
# custom_components/rainpoint/api/product_catalog.py rather than imported,
# because this script is standalone and only puts the component on sys.path
# once it is actually fetching. A test asserts the two stay in step.
UNCODED_VARIANT = "*"


def trim_catalog(raw: list[dict]) -> dict:
    """Trim a raw RainPoint productModel catalog to the committed snapshot shape.

    raw is the list returned by RainPointClient.get_product_catalog(): one
    entry per RainPoint model, each carrying a "model" name, an optional
    "modelCode", a model-level "portNumber", and a "dp" list of per-datapoint
    metadata dicts. Returns an object keyed by model string then by modelCode,
    whose values are {"portNumber": ..., "dp": [...]} records carrying whichever
    of _KEPT_PROVENANCE_FIELDS RainPoint supplied as booleans. RainPoint-prefixed
    models keep only their STA_/CTL_ dp entries, trimmed to _KEPT_DP_FIELDS;
    every other model, and every other identity namespace, is dropped.

    The model string alone is not a unique key: RainPoint maps some models to
    several modelCodes whose port counts genuinely differ (HIC801W is 0 ports
    under code 278 and 8 under 279), so a flat model-keyed object would silently
    keep whichever variant happened to come last. Entries with no modelCode land
    in the UNCODED_VARIANT bucket. Pure function: no I/O, no network.
    """
    trimmed: dict[str, dict[str, dict]] = {}
    for entry in raw:
        model = entry.get("model")
        if not model or not str(model).startswith(_MODEL_PREFIXES):
            continue
        model_code = entry.get("modelCode")
        variant = UNCODED_VARIANT if model_code is None else str(model_code)
        dp_entries = [dp for dp in (entry.get("dp") or []) if str(dp.get("identity") or "").startswith(_KEPT_IDENTITY_PREFIXES)]
        port_number = entry.get("portNumber")
        trimmed.setdefault(model, {})[variant] = {
            "portNumber": port_number if isinstance(port_number, int) and not isinstance(port_number, bool) else None,
            **{field: entry[field] for field in _KEPT_PROVENANCE_FIELDS if isinstance(entry.get(field), bool)},
            # Sort by dpCode so re-running against an unchanged RainPoint catalog
            # is deterministic, even if the RainPoint API does not guarantee a
            # stable dp array order across calls. Entries missing dpCode sort
            # last.
            "dp": sorted(
                ({field: dp.get(field) for field in _KEPT_DP_FIELDS} for dp in dp_entries),
                key=lambda d: (d.get("dpCode") is None, d.get("dpCode")),
            ),
        }
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
    """Return the committed snapshot, or {} when the file does not exist yet.

    Unlike the component-side loader this one is deliberately strict: a
    corrupt committed file should stop a maintainer run loudly rather than
    silently compare a fresh pull against an empty catalog and report every
    model as newly added.
    """
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _changed_fields(committed_variants: dict, fresh_variants: dict) -> set[str]:
    """Return the record keys that differ between two models' variant maps.

    Variants present on only one side count as a difference in every key that
    side declares, so an added or dropped modelCode is never reported as an
    empty change.
    """
    fields: set[str] = set()
    for variant in set(committed_variants) | set(fresh_variants):
        before = committed_variants.get(variant) or {}
        after = fresh_variants.get(variant) or {}
        fields |= {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    return fields


def _print_drift(committed: dict, fresh: dict) -> bool:
    """Print a human-readable drift summary. Returns True if any drift was found.

    Changed models are split by what actually moved. A snapshot regenerated
    after the trim starts keeping a new record key would otherwise report every
    model as changed with no way to tell that from real catalog drift, which is
    the difference between "commit this" and "read this carefully."
    """
    committed_models = set(committed)
    fresh_models = set(fresh)

    added = sorted(fresh_models - committed_models)
    removed = sorted(committed_models - fresh_models)
    changed = {
        model: _changed_fields(committed[model], fresh[model])
        for model in sorted(committed_models & fresh_models)
        if committed[model] != fresh[model]
    }

    if not added and not removed and not changed:
        print("No drift: the committed catalog matches a fresh pull.")
        return False

    if added:
        print(f"Models added upstream ({len(added)}): {', '.join(added)}")
    if removed:
        print(f"Models removed upstream ({len(removed)}): {', '.join(removed)}")

    substantive = sorted(model for model, fields in changed.items() if fields - set(_KEPT_PROVENANCE_FIELDS))
    metadata_only = sorted(model for model, fields in changed.items() if not fields - set(_KEPT_PROVENANCE_FIELDS))
    if substantive:
        detail = ", ".join(f"{model} ({', '.join(sorted(changed[model]))})" for model in substantive)
        print(f"Models with changed datapoints or ports ({len(substantive)}): {detail}")
    if metadata_only:
        print(f"Models changed only in RainPoint metadata ({len(metadata_only)}): {', '.join(metadata_only)}")
    return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the CLI parser and parse argv (defaults to sys.argv).

    Credentials default to their environment variables so the scheduled CI
    job needs no arguments at all.
    """
    parser = argparse.ArgumentParser(
        description="Regenerate the committed RainPoint product catalog from a live RainPoint pull.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff the committed catalog against a fresh live pull and exit nonzero on drift, without writing.",
    )
    parser.add_argument("--email", default=os.environ.get("RAINPOINT_EMAIL"), help="RainPoint account email (or RAINPOINT_EMAIL)")
    # There is deliberately no --password flag: a password passed on the command
    # line lands in argv, shell history, and CI process listings. The password
    # comes from RAINPOINT_PASSWORD for automation, or an interactive prompt.
    parser.add_argument(
        "--area-code",
        # "or" rather than a get() default: CI exports an unset optional secret
        # as an empty string, which would otherwise beat the default.
        default=os.environ.get("RAINPOINT_AREA_CODE") or "1",
        help="Phone-dial-style area code the RainPoint login expects, e.g. 1 for US (or RAINPOINT_AREA_CODE)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for login plus catalog fetch before giving up (default {_DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser.parse_args(argv)


def _resolve_password() -> str | None:
    """Return the password from RAINPOINT_PASSWORD, else prompt interactively.

    Returns None when the env var is unset and stdin is not a TTY, which is the
    non-interactive CI case: the caller reports a missing credential instead of
    blocking forever on a prompt nobody can answer.
    """
    password = os.environ.get("RAINPOINT_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        return None
    return getpass.getpass("RainPoint account password: ") or None


async def _fetch_trimmed_catalog(email: str, password: str, area_code: str, timeout_seconds: float) -> dict:
    """Log in, pull the RainPoint product catalog, and return it trimmed.

    aiohttp and the component client are imported here rather than at module
    scope: this is the only code path that needs them, and main() does not put
    the component on sys.path until it is about to fetch. That keeps
    trim_catalog importable on its own for tests.

    The fetch carries two deadlines because they bound different things. The
    session timeout caps a single request, since the component's client sets
    none and a bare session would inherit aiohttp's five-minute default. The
    catalog is around half a megabyte and normally arrives in under a second;
    five silent minutes reads as a wedged process and gets killed by hand long
    before it would ever fail on its own.

    That cap alone is not what --timeout promises. get_product_catalog issues
    two requests, the login and the catalog GET, and a per-request budget lets
    each of them spend the full value, so a slow login plus a slow fetch runs
    past the stated limit and then reports the wrong number. The outer wait_for
    makes --timeout the end-to-end deadline its help text describes.

    The progress lines exist for the same reason as the deadlines: without
    them, login, fetch, and diff are indistinguishable from a hang. Both go to
    stderr so --check output stays pipeable.
    """
    import aiohttp

    from custom_components.rainpoint.api.client import RainPointClient

    print(f"Logging in as {email} and fetching the RainPoint catalog (timeout {timeout_seconds:g}s)...", file=sys.stderr)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = RainPointClient(area_code, email, password, session)
        try:
            raw = await asyncio.wait_for(client.get_product_catalog(), timeout_seconds)
        except TimeoutError:
            print(
                f"Timed out after {timeout_seconds:g}s. Login or the catalog fetch did not finish in that "
                f"budget; retry, or raise the limit with --timeout.",
                file=sys.stderr,
            )
            raise
    print(f"Fetched {len(raw)} RainPoint model entries.", file=sys.stderr)
    return trim_catalog(raw)


def main(argv: list[str] | None = None) -> int:
    """Run the refresh, returning a process exit code.

    0 on a successful write or a clean --check, 1 on drift or a refused
    write, 2 when credentials are missing.
    """
    args = _parse_args(argv)

    password = _resolve_password()
    if not args.email or not password:
        print("RAINPOINT_EMAIL (env var or --email) and RAINPOINT_PASSWORD (env var) are required.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(_REPO_ROOT))
    trimmed = asyncio.run(_fetch_trimmed_catalog(args.email, password, args.area_code, args.timeout))

    if args.check:
        # An empty pull means the fetch failed, not that RainPoint dropped
        # every model. Without this guard the drift report would list the whole
        # committed catalog as "removed upstream" and bury the real cause.
        if not trimmed:
            print(
                "Live pull produced 0 kept models; treating as a fetch failure, not drift.",
                file=sys.stderr,
            )
            return 1
        committed = _load_committed_catalog(_CATALOG_PATH)
        drifted = _print_drift(committed, trimmed)
        return 1 if drifted else 0

    committed = _load_committed_catalog(_CATALOG_PATH)
    if not trimmed:
        print(
            "Refusing to write an empty catalog (live pull produced 0 kept models); "
            "check the RainPoint response before retrying.",
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
