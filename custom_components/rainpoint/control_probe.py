"""Opt-in encoding probe for HIC-family station control.

Why this exists at all
----------------------

Every valve this integration already drives declares one ``CTL_WATER``
datapoint *per port*, so ``control_work_mode(port=N)`` selects the zone by
addressing the matching catalog entry. The HIC family does not: HIC801W
declares a single ``CTL_WATER`` at ``dpPort`` 0 and covers all eight stations
with it, and HIC1200W does the same across twelve. The station number is
therefore encoded inside that datapoint's 2-byte payload, and nothing in this
repository establishes how.

No further status capture can settle it. ``dpCode`` 7 is write-only: the field
set observed across all 22 committed captures from two separate units is
``{1, 10, 19, 21, 30, 37, 38}``, and 7 is not in it. Asking an owner for more
readings, which is what unlocked read-only support, moves this exactly zero
distance.

So the experiment moves inside the integration. The owner presses a button, the
probe walks the candidate encodings, reads the station back after each one, and
records every attempt. They attach the diagnostics download to the support
thread. One round trip instead of a dozen, and no proxy, no certificate on a
phone, and no shell.

What keeps this safe
--------------------

This writes to someone else's hardware on their account, so the constraints are
structural rather than advisory:

- It is off unless the owner turns on an options toggle that ships off, so no
  ordinary user can reach it.
- The station walk targets station 3 (``HIC_PROBE_STATION``), asks for the
  shortest run that still answers the unit question, and sends the matching
  stop the moment a candidate works.
- Nothing here is optimistic. A candidate counts as working only when the
  controller itself reports that station running in one of its own state
  frames, never because the cloud returned success. Two frames can say so: the
  one these endpoints answer with, and the one read back after the settle.
  ``confirmed_by`` records which, because they are not equally available on
  this hardware and a reader should not have to guess.
- The walk stops at the first candidate that works, so the remaining writes
  never happen.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .api import RainPointApiError, decode_hic801w
from .const import (
    HIC_PROBE_MAX_ATTEMPTS,
    HIC_PROBE_RAIN_DELAY_DAYS,
    HIC_PROBE_RUN_VALUE,
    HIC_PROBE_SETTLE_SECONDS,
    HIC_PROBE_STATION,
)
from .coordinator import _resolve_addr_from_sid

_LOGGER = logging.getLogger(__name__)

# The catalog's own codes for the two control datapoints HIC801W variant 279
# declares. Read from the committed snapshot rather than invented: CTL_WATER is
# 2 bytes wide and CTL_SET_DELAY is 1, which is what fixes the width of every
# param encoded below.
DP_CODE_CTL_WATER = 7
DP_CODE_CTL_SET_DELAY = 11

# The marker every HIC801W status frame carries. Used to tell a state string
# apart from a bare acknowledgement before the decoder is asked to read it.
_FRAME_PREFIX = "10#"

ENDPOINT_WORK_MODE = "controlWorkMode"
ENDPOINT_WORK_MODE_DP = "controlWorkModeDP"

# The probe's own verdict vocabulary, kept to four words so the recorded run
# reads the same way to someone who has never seen this file.
OUTCOME_CONFIRMED = "confirmed"  # cloud accepted AND the controller reported it
OUTCOME_NO_EFFECT = "no_effect"  # cloud accepted, controller did not move
OUTCOME_REJECTED = "rejected"  # cloud refused the call
OUTCOME_UNREADABLE = "unreadable"  # call went out, neither frame could be decoded


@dataclass(frozen=True)
class ProbeCandidate:
    """One candidate command shape, fully specified before anything is sent.

    Declared as frozen data rather than built inline so the whole candidate
    space is a readable literal below: what this probe is willing to send is
    provable by reading the two lists, not by tracing a builder.
    """

    label: str
    endpoint: str
    port: int
    mode: int
    dp_code: int | None = None
    param: str | None = None
    duration: int | None = None
    addr_override: int | None = None
    note: str = ""


def _u16(value: int, *, big_endian: bool = False) -> str:
    """Return value as the 2-byte hex string CTL_WATER's declared width implies."""
    return value.to_bytes(2, "big" if big_endian else "little").hex().upper()


def _pair(low: int, high: int) -> str:
    """Return a 2-byte hex string from an explicit byte order."""
    return bytes((low & 0xFF, high & 0xFF)).hex().upper()


def station_candidates(station: int = HIC_PROBE_STATION, duration: int = HIC_PROBE_RUN_VALUE) -> list[ProbeCandidate]:
    """Return the ordered candidate encodings for starting one station.

    Ordered cheapest-hypothesis first. The two ``controlWorkMode`` shapes lead
    because that endpoint is the one every other valve in this integration
    already uses, so a hit there needs no new write path at all. The datapoint
    shapes follow, and within them the station-number readings precede the
    bitmask readings because the status side of this device reports a station
    number rather than a mask (``STA_WATER_ZONES`` b0 reads 03 for station 3,
    not 04), which makes the same convention on the command side the more
    likely of the two rather than merely the more convenient.

    The list is no longer expected to disambiguate anything. A real unit
    confirmed ``work_mode_port``, the first entry, so the walk exists now to
    reproduce that result and to fall through to the alternatives if the
    hardware in front of it turns out to differ. That is why the probe no
    longer runs against a station chosen to keep the encodings distinct: with
    ``HIC_PROBE_STATION`` at 1 several of these candidates encode to identical
    bytes, which would have been fatal to a walk that still had to tell them
    apart and costs nothing to one that does not. See the constant for why
    proving the stop now matters more than distinguishing the start.
    """
    mask = 1 << (station - 1)
    return [
        ProbeCandidate(
            label="work_mode_port",
            endpoint=ENDPOINT_WORK_MODE,
            port=station,
            mode=1,
            duration=duration,
            note="The shape every supported valve uses, with the station in port.",
        ),
        ProbeCandidate(
            label="work_mode_port_hub_addr",
            endpoint=ENDPOINT_WORK_MODE,
            port=station,
            mode=1,
            duration=duration,
            addr_override=0,
            note="Same, addressed to the hub rather than the sub-device.",
        ),
        ProbeCandidate(
            label="work_mode_port_no_duration",
            endpoint=ENDPOINT_WORK_MODE,
            port=station,
            mode=1,
            duration=None,
            note="Same, omitting duration entirely, as a hub-addressed call does.",
        ),
        ProbeCandidate(
            label="dp_station_le",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=station,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_u16(station),
            note="Station number as a little-endian 2-byte word.",
        ),
        ProbeCandidate(
            label="dp_station_be",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=station,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_u16(station, big_endian=True),
            note="Station number as a big-endian 2-byte word.",
        ),
        ProbeCandidate(
            label="dp_station_low_flag_high",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_pair(station, 1),
            note="Station in the low byte, on-flag in the high byte.",
        ),
        ProbeCandidate(
            label="dp_flag_low_station_high",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_pair(1, station),
            note="On-flag in the low byte, station in the high byte.",
        ),
        ProbeCandidate(
            label="dp_mask_le",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_u16(mask),
            note="Station as a one-hot bitmask, little-endian.",
        ),
        ProbeCandidate(
            label="dp_mask_be",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=_u16(mask, big_endian=True),
            note="Station as a one-hot bitmask, big-endian.",
        ),
        ProbeCandidate(
            label="dp_duration_param",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=station,
            mode=1,
            dp_code=DP_CODE_CTL_WATER,
            param=duration.to_bytes(4, "little").hex().upper(),
            note="The Bluetooth valve's own shape: station in port, duration in param.",
        ),
    ]


def rain_delay_candidates(days: int = HIC_PROBE_RAIN_DELAY_DAYS) -> list[ProbeCandidate]:
    """Return the ordered candidate encodings for setting a rain delay.

    Setting a rain delay waters nothing, so a wrong guess here costs nothing
    and a right one is visible in the vendor app immediately. That is what
    makes this stage worth running first: it settles the envelope questions
    (which endpoint this family takes, whether the body needs productKey and
    deviceName, how param is encoded for a known byte width, and what a success
    response looks like) at zero risk, and those answers narrow the station
    walk above.

    ``CTL_SET_DELAY`` is declared 1 byte wide, so the param encodings here are
    a single byte rather than the pair ``CTL_WATER`` takes.
    """
    return [
        ProbeCandidate(
            label="delay_dp_days",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_SET_DELAY,
            param=f"{days & 0xFF:02X}",
            note="Delay in days as the single byte the declared width implies.",
        ),
        ProbeCandidate(
            label="delay_dp_days_port_one",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=1,
            mode=1,
            dp_code=DP_CODE_CTL_SET_DELAY,
            param=f"{days & 0xFF:02X}",
            note="Same, in case this family counts ports from one rather than zero.",
        ),
        ProbeCandidate(
            label="delay_dp_days_word",
            endpoint=ENDPOINT_WORK_MODE_DP,
            port=0,
            mode=1,
            dp_code=DP_CODE_CTL_SET_DELAY,
            param=_u16(days),
            note="Same value padded to two bytes, in case the width is advisory.",
        ),
        ProbeCandidate(
            label="delay_work_mode_duration",
            endpoint=ENDPOINT_WORK_MODE,
            port=0,
            mode=1,
            duration=days,
            note="The plain endpoint, carrying the delay in the duration field.",
        ),
    ]


@dataclass
class ProbeRun:
    """The record of one probe press, and the thing the owner actually sends back."""

    kind: str
    station: int | None
    started_at: str | None = None
    finished_at: str | None = None
    confirmed_label: str | None = None
    stop_outcome: str | None = None
    baseline_frame: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the run as the plain dict the diagnostics dump carries."""
        return {
            "kind": self.kind,
            "station": self.station,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "confirmed_label": self.confirmed_label,
            "stop_outcome": self.stop_outcome,
            "baseline_frame": self.baseline_frame,
            "attempt_count": len(self.attempts),
            "attempts": self.attempts,
        }


def _request_record(candidate: ProbeCandidate, addr: int) -> dict[str, Any]:
    """Return the sanitized description of what was sent.

    deviceName and productKey are deliberately absent. They are the same two
    fields the diagnostics dump redacts by name everywhere else, and this
    record is written expressly to be attached to a public issue.
    """
    return {
        "endpoint": candidate.endpoint,
        "addr": candidate.addr_override if candidate.addr_override is not None else addr,
        "port": candidate.port,
        "mode": candidate.mode,
        "dp_code": candidate.dp_code,
        "param": candidate.param,
        "duration": candidate.duration,
    }


async def _send(client: Any, candidate: ProbeCandidate, *, mid: int, addr: int, device_name: str, product_key: str) -> str | None:
    """Issue one candidate command and return the cloud's own state string."""
    effective_addr = candidate.addr_override if candidate.addr_override is not None else addr
    if candidate.endpoint == ENDPOINT_WORK_MODE:
        return await client.control_work_mode(
            mid=mid,
            addr=effective_addr,
            device_name=device_name,
            product_key=product_key,
            port=candidate.port,
            mode=candidate.mode,
            duration=candidate.duration,
        )
    return await client.control_work_mode_dp(
        mid=mid,
        addr=effective_addr,
        device_name=device_name,
        product_key=product_key,
        port=candidate.port,
        mode=candidate.mode,
        param=candidate.param or "",
        dp_code=candidate.dp_code or DP_CODE_CTL_WATER,
    )


def _decode_frame(raw: Any) -> dict[str, Any]:
    """Return the station and run length one status frame reports.

    ``station`` is None for "could not read", never "no station running": the
    caller scores an unreadable frame as its own outcome rather than as a miss,
    so a decode failure cannot be mistaken for a candidate that did nothing.

    ``run_seconds`` is STA_DURATION as the controller reports it, and it is
    carried for one specific question the walk cannot otherwise answer: whether
    the ``duration`` argument this probe sends is read as seconds or as minutes.
    The first real run asked for 60 and the controller reported 3600, so the
    unit has to be settled before any duration reaches a user-facing control.
    Recording the number the controller reports next to the number that was
    asked for settles it without anyone decoding hex by hand.
    """
    if not isinstance(raw, str) or not raw.startswith(_FRAME_PREFIX):
        # The prefix check is what keeps the decoder off strings that were
        # never frames. Both control endpoints can answer with a plain word
        # rather than a state, and decode_hic801w answers an unparseable blob
        # with an error envelope logged at exception level. Handing it every
        # response would put a traceback in the owner's log for each attempt in
        # the walk, in a run whose whole purpose is to produce a log worth
        # reading.
        return {"station": None, "run_seconds": None}
    decoded = decode_hic801w(raw)
    station = decoded.get("current_station")
    run_seconds = decoded.get("run_duration_seconds")
    return {
        "station": station if isinstance(station, int) else None,
        "run_seconds": run_seconds if isinstance(run_seconds, int) else None,
    }


def _state_from_status(status: Any, addr: int) -> dict[str, Any]:
    """Return what one status response says about this controller.

    ``station`` and ``run_seconds`` carry _decode_frame's readings for the entry
    matching ``addr``, and both are None when there is nothing to read.

    ``frame`` is the read-back payload kept whole rather than decoded. The
    decoder deliberately reads none of STA_RAIN, STA_RH or STA_TS_DET, and
    STA_TS_DET is the field observed latching a station number after a run on a
    real unit, which makes it the one corroborating signal available when the
    station read-back loses its race against a controller that gives up on a
    station with no solenoid answering. Recording the frame keeps that evidence
    without teaching the decoder a field whose meaning is still unpinned, and
    the frames only ever reach the recorded run, never a log line.

    The entry is matched with _resolve_addr_from_sid rather than against a
    rebuilt ``f"D{addr}"`` string, and that is not a stylistic preference. The
    first real unit to run this probe reports its sub-device as ``D01``, so the
    string form matched nothing, every read-back scored unreadable, and a walk
    whose commands were in fact working recorded no winner. The coordinator has
    always resolved these ids numerically and says so where it does; sharing
    that one definition is what stops the two from disagreeing again.
    """
    unread: dict[str, Any] = {"station": None, "run_seconds": None, "frame": None}
    if not isinstance(status, dict):
        return unread
    for entry in status.get("subDeviceStatus") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        if _resolve_addr_from_sid(entry["id"]) != addr:
            continue
        raw = entry.get("value")
        if not isinstance(raw, str):
            return unread
        return {**_decode_frame(raw), "frame": raw}
    return unread


async def _read_state(client: Any, mid: int, addr: int) -> dict[str, Any]:
    """Read the controller back through a direct status call.

    Deliberately not a coordinator refresh: the coordinator polls on its own
    two-minute interval, and waiting for it between candidates would stretch a
    ten-candidate walk past twenty minutes. This asks the cloud directly and
    decodes the one record it needs.
    """
    try:
        status = await client.get_device_status(mid)
    except (RainPointApiError, TimeoutError, OSError) as err:
        # WARNING rather than DEBUG for the same reason the walk's own lines
        # are: an owner running this is being asked for their log, and a
        # read-back that never happened is the difference between "this
        # encoding did nothing" and "nobody looked".
        _LOGGER.warning("HIC probe: status read-back failed: %s", type(err).__name__)
        return {"station": None, "run_seconds": None, "frame": None}
    return _state_from_status(status, addr)


async def _attempt(
    client: Any,
    candidate: ProbeCandidate,
    *,
    mid: int,
    addr: int,
    device_name: str,
    product_key: str,
    expect_station: int | None,
) -> dict[str, Any]:
    """Send one candidate, read the controller back, and score the result."""
    record: dict[str, Any] = {
        "label": candidate.label,
        "note": candidate.note,
        "request": _request_record(candidate, addr),
    }
    try:
        response_state = await _send(client, candidate, mid=mid, addr=addr, device_name=device_name, product_key=product_key)
    except RainPointApiError as err:
        record["outcome"] = OUTCOME_REJECTED
        record["error"] = str(err)
        return record
    except (TimeoutError, OSError) as err:
        # A transport failure is not the device declining the command, so it is
        # recorded as a rejection with its type rather than scored as evidence
        # that this encoding is wrong.
        record["outcome"] = OUTCOME_REJECTED
        record["error"] = f"transport: {type(err).__name__}"
        return record

    record["cloud_state"] = response_state
    reported = _decode_frame(response_state)
    record["station_in_response"] = reported["station"]
    record["run_seconds_in_response"] = reported["run_seconds"]
    if expect_station is None:
        # Variant 279 declares no STA_ counterpart for a rain delay, so nothing
        # here can be scored and the outcome stays NO_EFFECT: this stage still
        # cannot say a candidate worked.
        #
        # It can preserve the evidence, though, and that is new. Every frame is
        # recorded even though nothing reads them yet, because the field that
        # moves when a delay is set can be identified separately, by the owner
        # setting a delay in the vendor app and sending the frame that results.
        # Once that field is known, these frames say which candidate moved it
        # and the first one that did is the answer. Recording them costs one
        # status call each and turns a second round trip into a comparison.
        await asyncio.sleep(HIC_PROBE_SETTLE_SECONDS)
        delay_state = await _read_state(client, mid, addr)
        record["outcome"] = OUTCOME_NO_EFFECT
        record["read_back"] = "recorded_not_scored"
        record["frame_after"] = delay_state["frame"]
        return record

    await asyncio.sleep(HIC_PROBE_SETTLE_SECONDS)
    read_back = await _read_state(client, mid, addr)
    station = read_back["station"]
    record["station_after"] = station
    record["run_seconds_after"] = read_back["run_seconds"]
    record["frame_after"] = read_back["frame"]
    if station == expect_station:
        record["outcome"] = OUTCOME_CONFIRMED
        record["confirmed_by"] = "read_back"
    elif reported["station"] == expect_station:
        # The read-back is not the only witness, and on this device it is not
        # even the reliable one. These endpoints answer with the controller's
        # own state frame, and on the first real run that frame already showed
        # the commanded station running before the settle had elapsed. The
        # read-back that follows loses a race the owner described plainly: a
        # station with no solenoid answering is dropped again within seconds,
        # so waiting to ask is how a working encoding scores as a miss.
        #
        # This stays honest about what "confirmed" has always meant here,
        # because the state frame is the controller's reading of itself rather
        # than the cloud's acknowledgement of the call. A response that merely
        # succeeded still confirms nothing; ``confirmed_by`` records which
        # witness spoke so the distinction survives into the report.
        record["outcome"] = OUTCOME_CONFIRMED
        record["confirmed_by"] = "response"
    elif station is None:
        record["outcome"] = OUTCOME_UNREADABLE
    else:
        record["outcome"] = OUTCOME_NO_EFFECT
    return record


def _log_attempt(record: dict[str, Any], position: int, total: int) -> None:
    """Announce one scored attempt at a level a plain log download captures.

    WARNING rather than INFO or DEBUG, and that is the whole point of this
    function. A default Home Assistant install records WARNING and above, so an
    owner who presses the button and sends their log has already sent the
    answer; at INFO the first real run came back with a log carrying nothing but
    the line saying the buttons had loaded. The level is defensible on its own
    terms too: nothing here runs unless a human turned an off-by-default option
    on and pressed a button that writes commands to their hardware.

    This module's own vocabulary and integers only: the candidate label, the
    verdict, which witness confirmed it, the station each witness reported, and
    the run length the controller reported against the one that was asked for.
    No frame, no cloud message, no addressing field, so the line stays inside
    the rule the cloud-record paths already follow.

    The run lengths are on the line rather than in the record alone because
    this is the surface that survives everything. A owner who sends nothing but
    a plain log download has still sent the seconds-or-minutes answer.
    """
    _LOGGER.warning(
        "HIC probe: attempt %d/%d %s -> %s (by=%s, station_after=%s, station_in_response=%s, asked_run=%s, reported_run=%s)",
        position,
        total,
        record.get("label"),
        record.get("outcome"),
        record.get("confirmed_by"),
        record.get("station_after"),
        record.get("station_in_response"),
        (record.get("request") or {}).get("duration"),
        record.get("run_seconds_in_response") if record.get("run_seconds_after") is None else record.get("run_seconds_after"),
    )


def _stop_would_prove_anything(start_record: dict[str, Any]) -> bool:
    """Return whether a station reading off after the stop can be credited to it.

    It can only be credited when the run the controller reported was still
    supposed to be going when the stop was sent. The probe waits
    HIC_PROBE_SETTLE_SECONDS before reading back and again before sending the
    stop, so a run shorter than that had already expired on its own and the
    station being off afterwards says nothing about the command.

    A run length that could not be read at all returns False rather than True.
    The question is whether there is positive evidence the run was still alive,
    and an unreadable frame is not that.
    """
    for key in ("run_seconds_after", "run_seconds_in_response"):
        reported = start_record.get(key)
        if isinstance(reported, int):
            return reported > HIC_PROBE_SETTLE_SECONDS
    return False


def _stop_candidate(confirmed: ProbeCandidate, station: int) -> ProbeCandidate:
    """Return the inverse of the candidate that worked.

    Built from the confirmed shape rather than guessed independently: whatever
    encoding started the station is the one most likely to stop it with mode 0,
    and recording whether that holds is what a valve entity needs next.
    """
    return ProbeCandidate(
        label=f"{confirmed.label}_stop",
        endpoint=confirmed.endpoint,
        port=confirmed.port,
        mode=0,
        dp_code=confirmed.dp_code,
        param=confirmed.param,
        duration=0 if confirmed.duration is not None else None,
        addr_override=confirmed.addr_override,
        note=f"Stop for station {station}, mirroring the encoding that worked.",
    )


async def async_run_probe(
    client: Any,
    sensor_info: dict,
    *,
    kind: str,
    now: str,
) -> ProbeRun:
    """Walk the candidate encodings for one stage and return the recorded run.

    ``kind`` is ``"rain_delay"`` or ``"station"``. The station walk stops at the
    first candidate the controller confirms and immediately sends the matching
    stop; the rain-delay walk runs every candidate, because it has no read-back
    to stop on and each one is harmless.

    The rain-delay walk reads one frame before it sends anything. That baseline
    is what the frames recorded after each candidate are compared against, and
    it has to come from the same session on the same unit: the committed
    captures are all from units with no delay set and cannot show which field a
    delay moves on this one.
    """
    mid = sensor_info["mid"]
    addr = sensor_info["addr"]
    device_name = sensor_info.get("device_name") or ""
    product_key = sensor_info.get("product_key") or ""

    if kind == "station":
        candidates = station_candidates()
        expect_station: int | None = HIC_PROBE_STATION
    else:
        candidates = rain_delay_candidates()
        expect_station = None

    run = ProbeRun(kind=kind, station=expect_station, started_at=now)
    if expect_station is None:
        run.baseline_frame = (await _read_state(client, mid, addr))["frame"]
    walk = candidates[:HIC_PROBE_MAX_ATTEMPTS]
    for position, candidate in enumerate(walk, start=1):
        record = await _attempt(
            client,
            candidate,
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            expect_station=expect_station,
        )
        run.attempts.append(record)
        _log_attempt(record, position, len(walk))
        if record["outcome"] != OUTCOME_CONFIRMED:
            continue

        run.confirmed_label = candidate.label
        stop = _stop_candidate(candidate, expect_station or 0)
        stop_record = await _attempt(
            client,
            stop,
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            expect_station=expect_station,
        )
        # The stop is scored inverted: the station going quiet is the success
        # here, so a read-back that still reports it running is what a reader
        # needs to see called out.
        #
        # An unreadable frame is emphatically not success. It was counted as
        # one while the read-back was matching sub-device ids by a rebuilt
        # string and therefore never matching at all, which is exactly the
        # combination that would have reported a stop command as working
        # without a single frame supporting it. Either witness reading 0
        # counts; neither being readable leaves this False.
        #
        # Conclusiveness is scored separately and first, because a station can
        # read off for a reason that has nothing to do with the stop. A run
        # already over by the time the stop went out is the obvious one, and it
        # is not hypothetical: it is what station 3 did on the reporter's unit
        # every time, and why the probe moved to a wired station. Recording
        # "off, but it proves nothing" is the difference between an answer and
        # a coincidence that reads like one.
        stop_record["stop_conclusive"] = _stop_would_prove_anything(record)
        stop_record["stop_succeeded"] = stop_record["stop_conclusive"] and 0 in (
            stop_record.get("station_after"),
            stop_record.get("station_in_response"),
        )
        run.attempts.append(stop_record)
        _log_attempt(stop_record, len(run.attempts), len(walk) + 1)
        run.stop_outcome = stop_record["outcome"]
        break

    return run
