#!/usr/bin/env python3
"""Answer the new-model triage questions from a reporter's diagnostics file.

When someone files a new device support request, three questions decide what
happens next, and all three are answerable from the attachment alone:

1. Does the payload already decode through a decoder this integration ships?
2. What does the bundled product catalog say the device is (variant, ports)?
3. Which control path would its zones take, if any?

Answering them by reading the JSON works and is what produced HTV445FRF
support, but it is mechanical and it is easy to miss that a payload belongs to
a family already handled. This runs the same checks the integration would and
prints the verdict.

Reads a local file and nothing else: no credentials, no cloud calls, no writes.

Usage:
    python scripts/triage_diagnostics.py rainpoint-01M0KJ...json

    # Only the devices whose model matches, on an account-wide dump.
    python scripts/triage_diagnostics.py dump.json --model HTV445FRF

    # Triage a bare payload with no file, when that is all the issue carries.
    python scripts/triage_diagnostics.py --payload '11#299F...' --model HTV445FRF

    # Confirm the script itself still works, against a known capture.
    python scripts/triage_diagnostics.py --selftest

The decoder trial is the part to read with judgement. A decoder that returns
fields on a foreign payload has not proved anything: the framings overlap, so
several will parse the same bytes. Treat a match as a candidate to check
against what the RainPoint app shows, never as a decision.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The dp identity a catalog variant declares for an RF-commanded water port.
# Its Bluetooth counterpart lives in api/trust.py, which owns that question.
_RF_CONTROL_IDENTITY = "CTL_WATER"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Triage a RainPoint diagnostics file for new-model support.",
    )
    parser.add_argument("path", nargs="?", help="Path to a downloaded diagnostics JSON file.")
    parser.add_argument("--model", help="Only report devices whose model matches this string.")
    parser.add_argument("--payload", help="Triage this raw payload directly instead of reading a file.")
    parser.add_argument("--model-code", help="modelCode to pair with --payload, when the issue names one.")
    parser.add_argument("--selftest", action="store_true", help="Run against a known capture and check the output.")
    return parser.parse_args(argv)


def _devices_from_dump(dump: dict) -> list[dict]:
    """Return one flat record per sub-device the dump carries.

    Handles both shapes the integration produces: the account-wide dump keyed
    under "sensors", and a single device's dump, which carries the same entry
    shape under "device" plus whichever sensors belong to it.

    Home Assistant wraps whatever the integration returns under a "data" key,
    alongside its own "home_assistant" and "integration_manifest" blocks, so a
    file downloaded from the UI is unwrapped first. A payload passed through
    some other route arrives unwrapped and still works.
    """
    inner = dump.get("data")
    if isinstance(inner, dict) and ("sensors" in inner or "device" in inner):
        dump = inner
    sensors = dump.get("sensors")
    if isinstance(sensors, dict) and sensors:
        return [entry for entry in sensors.values() if isinstance(entry, dict)]
    device = dump.get("device")
    # A device-scoped dump whose sensors map came back empty still carries a
    # "device" block, but that block is identity only: no model and no payload.
    # Reporting it would print an all-unknown record that reads as a finding.
    if isinstance(device, dict) and ("model" in device or "raw_status" in device):
        return [device]
    return []


def _payload_of(entry: dict) -> str | None:
    raw_status = entry.get("raw_status")
    if not isinstance(raw_status, dict):
        return None
    value = raw_status.get("value")
    return value if isinstance(value, str) and value else None


def _catalog_summary(model: str | None, model_code) -> list[str]:
    """Describe what the committed catalog knows about this variant."""
    from custom_components.rainpoint.api.product_catalog import (
        get_catalog_entry,
        get_catalog_port_number,
        get_catalog_variant_codes,
    )

    codes = get_catalog_variant_codes(model)
    if not codes:
        return ["catalog: model absent from the committed snapshot"]

    lines = [f"catalog: model present, variants {', '.join(codes)}"]
    dp_entries = get_catalog_entry(model, model_code)
    if dp_entries is None:
        if model_code is None:
            lines.append(
                "  variant unresolved: several coded variants and the device did not say which. "
                "Ask the reporter for the modelCode, or read it from the dump's sub-device record."
            )
        else:
            # A known model reporting a code the snapshot has never seen is the
            # shape a genuinely new variant arrives in, so name it rather than
            # sending the reporter to fetch a code they already supplied.
            lines.append(
                f"  variant unresolved: the device reports modelCode {model_code}, which this snapshot "
                "does not list. Refresh the catalog before deciding this device is unknown."
            )
        return lines

    ports = get_catalog_port_number(model, model_code)
    lines.append(f"  declared ports: {ports if ports is not None else 'not stated'}")
    identities = [str(entry["identity"]) for entry in dp_entries if isinstance(entry, dict) and entry.get("identity")]
    if identities:
        lines.append(f"  dp identities: {', '.join(sorted(set(identities)))}")
    return lines


def _control_route(model: str | None, model_code) -> str:
    """Name the endpoint this model's zones would command through."""
    from custom_components.rainpoint.api.product_catalog import get_catalog_entry
    from custom_components.rainpoint.api.trust import has_bluetooth_control_identity

    if has_bluetooth_control_identity(model, model_code):
        return "controlWorkModeDP (Bluetooth datapoint endpoint, CTL_BT_WATER)"

    dp_entries = get_catalog_entry(model, model_code) or []
    water_ports = sum(1 for entry in dp_entries if isinstance(entry, dict) and entry.get("identity") == _RF_CONTROL_IDENTITY)
    if water_ports:
        return f"controlWorkMode (RF, {water_ports} {_RF_CONTROL_IDENTITY} port(s))"
    return "none declared: read-only unless a probe proves otherwise"


def _generic_lines(payload: str, model: str | None, model_code) -> list[str]:
    """Render the model-agnostic decode the reporter's dump already carries."""
    from custom_components.rainpoint.api.generic_decoder import decode_generic

    generic = decode_generic(payload, model=model, model_code=model_code)
    if generic.get("error") and not generic.get("fields"):
        return [f"generic decode: declined ({generic['error']})"]

    fields = generic.get("fields") or []
    lines = [f"generic decode: {len(fields)} field(s)"]
    if generic.get("error"):
        lines.append(f"  header only, body declined ({generic['error']})")
    for field in fields:
        catalog = field.get("catalog") or {}
        port = catalog.get("dp_port")
        suffix = f"  [zone {port}]" if port else ""
        mismatch = "  WIDTH MISMATCH" if catalog.get("width_mismatch") else ""
        lines.append(f"  {field.get('name')}: raw={field.get('raw')} value={field.get('value')}{suffix}{mismatch}")
    return lines


def _clip(value, limit: int = 90) -> str:
    """Shorten a decoder's own message, which often echoes the whole payload."""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _decoder_candidates() -> dict:
    """Map each distinct registered decoder to the models that use it."""
    from custom_components.rainpoint.coordinator import DECODER_REGISTRY

    by_func: dict = {}
    for model, func in DECODER_REGISTRY.items():
        by_func.setdefault(func, []).append(model)
    return by_func


def _decoder_trial(payload: str, model: str | None = None) -> list[str]:
    """Run every registered decoder against the payload and report what parsed.

    The model's own registered decoder is marked so it is never lost in the
    ranking. It gets lost easily: on the HCS0528ARF pool capture the right
    decoder returns fewer keys than two valve decoders that happily parse the
    same bytes, and it sorts third.

    ponytail: scores a result by how many keys it returns, which is a crude
    proxy for "read something real". Good enough to rank candidates for a human
    to check; replace it with a per-family sanity assertion if it ever picks a
    confident wrong answer.
    """
    from custom_components.rainpoint.coordinator import DECODER_REGISTRY

    registered = DECODER_REGISTRY.get(model) if model else None
    results = []
    for func, models in _decoder_candidates().items():
        try:
            decoded = func(payload)
        except Exception as exc:  # a foreign payload is expected to break decoders
            results.append((0, func is registered, func.__name__, models, f"raised {type(exc).__name__}: {_clip(exc)}"))
            continue
        if not isinstance(decoded, dict) or not decoded:
            results.append((0, func is registered, func.__name__, models, "returned nothing"))
            continue
        if decoded.get("error"):
            # A decoder that parsed far enough to report its own failure has
            # declined the payload as surely as one that raised.
            results.append((0, func is registered, func.__name__, models, f"declined: {_clip(decoded['error'])}"))
            continue
        keys = [key for key in decoded if key not in {"type", "model", "raw_value", "generic"}]
        detail = ", ".join(sorted(keys)) or "no readings"
        results.append((len(keys), func is registered, func.__name__, models, detail))

    # The model's own decoder sorts first whatever it scored, so a right answer
    # that reads fewer fields than a wrong one is still the first line read.
    results.sort(key=lambda row: (not row[1], -row[0]))
    lines = ["decoder trial (a match is a candidate, not a decision):"]
    for score, is_registered, name, models, detail in results:
        if is_registered:
            marker = "  ##"
        elif score:
            marker = "  ->"
        else:
            marker = "    "
        suffix = "  <- the registry already maps this model here" if is_registered else ""
        lines.append(f"{marker} {name} ({', '.join(sorted(models))}): {detail}{suffix}")
    return lines


def _report(model: str | None, model_code, payload: str | None, extra: dict) -> list[str]:
    from custom_components.rainpoint.api.trust import is_hand_written_model

    lines = [f"=== {model or 'unknown model'} (modelCode {model_code if model_code is not None else 'not stated'})"]
    for label, key in (("firmware", "firmware_version"), ("hub paired", "hub_paired")):
        if key in extra:
            lines.append(f"{label}: {extra[key]}")
    lines.append(
        "already supported: yes, hand-written decoder"
        if is_hand_written_model(model)
        else "already supported: no, this model would fall through to the generic path"
    )
    lines.extend(_catalog_summary(model, model_code))
    lines.append(f"control route: {_control_route(model, model_code)}")

    if not payload:
        lines.append("payload: none in this record. A device that returns no status is itself the finding.")
        return lines

    lines.append(f"payload: {payload}")
    lines.extend(_generic_lines(payload, model, model_code))
    lines.extend(_decoder_trial(payload, model))
    return lines


def _salvage(text: str) -> list[dict]:
    """Scrape model and payload pairs out of a file that will not parse.

    Reporters redact diagnostics by hand before attaching them, which is how
    the HCS008FRF captures arrived: deleted blocks left dangling commas and
    inline "## REMOVING FOR PRIVACY ##" markers, so the file is no longer JSON.
    The three things triage needs survive that as plain text, and refusing the
    file outright wastes a capture the reporter cannot always retake.

    Pairs each payload with the nearest preceding model and modelCode, which
    holds because the integration writes them in that order within a record.
    """
    records: list[dict] = []
    model: str | None = None
    model_code: str | None = None
    for match in re.finditer(
        r'"model"\s*:\s*"(?P<model>[^"]+)"'
        r'|"model_code"\s*:\s*"?(?P<code>[0-9]+)"?'
        r'|"value"\s*:\s*"(?P<payload>[0-9]+#[^"]*)"',
        text,
    ):
        if match.group("model"):
            model = match.group("model")
        elif match.group("code"):
            model_code = match.group("code")
        else:
            records.append({"model": model, "model_code": model_code, "payload": match.group("payload")})
    return records


def _triage_file(path: Path, model_filter: str | None) -> int:
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"Could not read {path}: {exc}", file=sys.stderr)
        return 1

    try:
        dump = json.loads(text)
    except ValueError as exc:
        print(f"{path.name} is not valid JSON ({exc}).", file=sys.stderr)
        print("Reading it as text instead. A hand-redacted dump usually lands here.\n", file=sys.stderr)
        salvaged = [record for record in _salvage(text) if not model_filter or record["model"] == model_filter]
        if not salvaged:
            print("Nothing recoverable: no model and payload pair found in the text.", file=sys.stderr)
            return 1
        for record in salvaged:
            print("\n".join(_report(record["model"], record["model_code"], record["payload"], {})))
            print()
        return 0

    inner = dump["data"] if isinstance(dump.get("data"), dict) else dump
    version = (inner.get("integration") or {}).get("version")
    if version:
        print(f"dump written by integration version {version}\n")

    devices = _devices_from_dump(dump)
    if not devices:
        print("No sub-device records in this file. An account with hubs only produces this.", file=sys.stderr)
        return 1

    reported = 0
    for entry in devices:
        model = entry.get("model")
        if model_filter and model != model_filter:
            continue
        reported += 1
        print("\n".join(_report(model, entry.get("model_code"), _payload_of(entry), entry)))
        print()

    if not reported:
        print(f"No device in this file reports model {model_filter}.", file=sys.stderr)
        return 1
    return 0


def _selftest() -> int:
    """Check the pipeline against the HTV445FRF capture from issue #203."""
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from payload_samples import SAMPLE_HTV445_TLV_PAYLOAD  # type: ignore[import-not-found]

    lines = _report("HTV445FRF", None, SAMPLE_HTV445_TLV_PAYLOAD, {})
    output = "\n".join(lines)
    print(output)

    generic_line = next((line for line in lines if line.startswith("generic decode:")), "")
    # Checked rather than asserted: python -O drops assert statements, and a
    # self-test that prints "ok" without having checked anything is worse than
    # no self-test.
    failures = [
        message
        for message, passed in (
            ("HTV445FRF should read as hand-written", "already supported: yes" in output),
            ("4-zone valve should route to the RF endpoint", "controlWorkMode (RF" in output),
            ("the HTV213/245 family decoder should be trialled", "decode_htv213frf_valve" in output),
            ("the capture should yield generic fields", bool(generic_line) and "0 field(s)" not in generic_line),
        )
        if not passed
    ]
    if failures:
        print("\nselftest: FAILED", file=sys.stderr)
        for message in failures:
            print(f"  {message}", file=sys.stderr)
        return 1

    print("\nselftest: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.path.insert(0, str(_REPO_ROOT))

    # The decoders log a traceback whenever they decline a payload, which is
    # the normal case here: the trial runs every one of them against a payload
    # only one family owns. The script reports each decline itself, so the
    # integration's own logging is noise on this path.
    logging.getLogger("custom_components.rainpoint").setLevel(logging.CRITICAL)

    if args.selftest:
        return _selftest()

    if args.payload:
        print("\n".join(_report(args.model, args.model_code, args.payload, {})))
        return 0

    if not args.path:
        print("Give a diagnostics file, --payload, or --selftest.", file=sys.stderr)
        return 2

    return _triage_file(Path(args.path), args.model)


if __name__ == "__main__":
    raise SystemExit(main())
