"""Tests for the opt-in HIC control-encoding probe.

Two properties matter more than line coverage here, because this module writes
commands to hardware the maintainer does not own:

- Nothing is ever scored as working on the cloud's say-so. A candidate is
  confirmed only when one of the controller's own state frames reports the
  station, whether that is the frame the endpoint answered with or the one read
  back after the settle, and every other path has its own outcome word.
- The walk stops the moment something works, and sends the stop.

Both are asserted directly rather than inferred from a call count.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint.api import RainPointApiError, decode_hic801w
from custom_components.rainpoint.const import (
    HIC_PROBE_RUN_VALUE,
    HIC_PROBE_SETTLE_SECONDS,
    HIC_PROBE_STATION,
)
from custom_components.rainpoint.control_probe import (
    DP_CODE_CTL_SET_DELAY,
    DP_CODE_CTL_WATER,
    ENDPOINT_WORK_MODE,
    ENDPOINT_WORK_MODE_DP,
    OUTCOME_CONFIRMED,
    OUTCOME_NO_EFFECT,
    OUTCOME_REJECTED,
    OUTCOME_UNREADABLE,
    ProbeCandidate,
    ProbeRun,
    _pair,
    _state_from_status,
    _stop_candidate,
    _stop_would_prove_anything,
    _u16,
    async_run_probe,
    rain_delay_candidates,
    station_candidates,
)
from tests.payload_samples import (
    SAMPLE_HIC801W_IDLE_PAYLOAD,
    SAMPLE_HIC801W_PROBE_RESPONSE_STATION3,
    SAMPLE_HIC801W_REPORTER_FRAMES,
)

# A captured frame showing the station the probe actually targets, resolved
# through the constant rather than hard-coded. The probe moved from station 3
# to station 1 once the encoding question closed and proving the stop became
# the point, and every test that drives a walk has to follow it: a frame
# reporting a station nobody commanded confirms nothing, so the whole suite
# would have gone quietly green on a walk that never confirmed anything.
PROBED_STATION_PAYLOAD = SAMPLE_HIC801W_REPORTER_FRAMES[f"2026-08-10 st{HIC_PROBE_STATION}"]

SENSOR_INFO = {
    "mid": 85577,
    "addr": 1,
    "device_name": "hub-device-name",
    "product_key": "hub-product-key",
    "model": "HIC801W",
}


def _status(payload: str, addr: int | str = 1) -> dict:
    """Return a getDeviceStatus body carrying one sub-device reading.

    ``addr`` accepts a string so a test can pin the exact id spelling a real
    unit sends. The first HIC801W to run the probe reports ``D01``, not ``D1``.
    """
    return {"subDeviceStatus": [{"id": f"D{addr}", "value": payload, "time": 1}]}


def _client(*, statuses=None, send=None) -> MagicMock:
    """Return a client double whose two write endpoints and one read are recorded."""
    client = MagicMock()
    client.control_work_mode = AsyncMock(side_effect=send) if send else AsyncMock(return_value="ok")
    client.control_work_mode_dp = AsyncMock(side_effect=send) if send else AsyncMock(return_value="ok")
    client.get_device_status = AsyncMock(side_effect=statuses if statuses else lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD))
    return client


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """Make the inter-attempt settle instant without changing how long it is.

    The sleep is stubbed rather than the constant zeroed, and the difference
    matters. HIC_PROBE_SETTLE_SECONDS is not only how long the probe waits, it
    is also the threshold a stop is judged against: a run shorter than the
    settle had already expired before the stop went out and cannot be credited
    to it. Zeroing the constant made every run look like it was still going,
    which quietly disabled the rule in exactly the tests written to prove it.
    """

    async def _instant(_seconds):
        return None

    monkeypatch.setattr("custom_components.rainpoint.control_probe.asyncio.sleep", _instant)


class TestEncodingHelpers:
    """The byte-level helpers the candidate list is built from."""

    def test_u16_little_endian_puts_the_low_byte_first(self):
        assert _u16(3) == "0300"

    def test_u16_big_endian_reverses_it(self):
        assert _u16(3, big_endian=True) == "0003"

    def test_pair_is_explicit_about_byte_order(self):
        assert _pair(3, 1) == "0301"
        assert _pair(1, 3) == "0103"


class TestCandidateSpace:
    """Properties of the candidate lists themselves, not of running them."""

    def test_every_candidate_is_distinguishable_at_a_distinguishing_station(self):
        """No two candidates send the same thing when the station allows it.

        Station 1 collapses the station-number and bitmask readings onto the
        same byte and both byte orders onto the same pair, so a hit there would
        not say which encoding was right. That mattered while the encoding was
        unknown and the probe ran against station 3 for exactly this reason.
        The property still holds of the generator, and is asserted against a
        station that can express it rather than against whichever station the
        probe currently targets.
        """
        shapes = [
            (c.endpoint, c.port, c.mode, c.dp_code, c.param, c.duration, c.addr_override) for c in station_candidates(station=3)
        ]

        assert len(set(shapes)) == len(shapes)

    def test_the_probed_station_no_longer_needs_to_distinguish_anything(self):
        """The collapse at station 1 is deliberate, so it is asserted rather than tripped over.

        A real unit confirmed the first candidate, so the walk no longer has to
        tell the alternatives apart, and the station is chosen for whether the
        stop can be proved on it instead. If this ever starts passing by
        accident because the constant moved back, the test above is the one
        that still guards the generator.
        """
        shapes = [(c.endpoint, c.port, c.mode, c.dp_code, c.param, c.duration, c.addr_override) for c in station_candidates()]

        assert HIC_PROBE_STATION == 1
        assert len(set(shapes)) < len(shapes)
        assert station_candidates()[0].label == "work_mode_port"

    def test_station_candidates_only_ever_address_the_catalog_datapoint(self):
        """No candidate invents a dpCode the catalog does not declare for this model."""
        codes = {c.dp_code for c in station_candidates() if c.dp_code is not None}

        assert codes == {DP_CODE_CTL_WATER}

    def test_rain_delay_candidates_only_address_the_delay_datapoint(self):
        codes = {c.dp_code for c in rain_delay_candidates() if c.dp_code is not None}

        assert codes == {DP_CODE_CTL_SET_DELAY}

    def test_every_station_candidate_starts_rather_than_stops(self):
        """A walk that sent mode 0 anywhere would score a stop as a failed start."""
        assert {c.mode for c in station_candidates()} == {1}

    def test_no_station_candidate_asks_for_longer_than_the_capped_run(self):
        """The cap is what makes a rejected stop harmless, so nothing may exceed it."""
        durations = [c.duration for c in station_candidates() if c.duration is not None]

        assert durations and max(durations) <= HIC_PROBE_RUN_VALUE

    def test_plain_endpoint_candidates_lead_the_walk(self):
        """The endpoint every supported valve already uses is tried before the datapoint one."""
        endpoints = [c.endpoint for c in station_candidates()]

        assert endpoints[0] == ENDPOINT_WORK_MODE
        assert ENDPOINT_WORK_MODE_DP in endpoints
        assert endpoints.index(ENDPOINT_WORK_MODE_DP) > endpoints.index(ENDPOINT_WORK_MODE)


class TestStationWalk:
    """Running the station stage against a controller double."""

    @pytest.mark.asyncio
    async def test_a_controller_that_ignores_everything_confirms_nothing(self):
        """Every candidate is tried, and none is scored as working."""
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label is None
        assert len(run.attempts) == len(station_candidates())
        assert {a["outcome"] for a in run.attempts} == {OUTCOME_NO_EFFECT}

    @pytest.mark.asyncio
    async def test_the_walk_stops_at_the_first_candidate_that_works(self):
        """The remaining candidates are never sent, which is the point of stopping."""
        calls = {"n": 0}

        def statuses(mid):
            calls["n"] += 1
            # The third read-back is the first that reports the station: the
            # first two candidates did nothing.
            if calls["n"] >= 3:
                return _status(PROBED_STATION_PAYLOAD)
            return _status(SAMPLE_HIC801W_IDLE_PAYLOAD)

        client = _client(statuses=statuses)

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label == station_candidates()[2].label
        # Three starts plus the one stop, and nothing after it.
        assert len(run.attempts) == 4
        assert run.attempts[2]["outcome"] == OUTCOME_CONFIRMED

    @pytest.mark.asyncio
    async def test_a_confirmed_candidate_is_followed_by_its_own_inverse(self):
        """The stop mirrors the encoding that worked rather than guessing separately."""
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        first = station_candidates()[0]
        stop = run.attempts[-1]
        assert stop["label"] == f"{first.label}_stop"
        assert stop["request"]["mode"] == 0
        assert stop["request"]["endpoint"] == first.endpoint
        assert stop["request"]["param"] == first.param

    @pytest.mark.asyncio
    async def test_a_stop_the_controller_ignores_is_recorded_as_not_succeeding(self):
        """The station still reading as running after the stop is the thing to surface."""
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[-1]["stop_succeeded"] is False

    @pytest.mark.asyncio
    async def test_a_stop_the_controller_honours_is_recorded_as_succeeding(self):
        reads = iter([_status(PROBED_STATION_PAYLOAD), _status(SAMPLE_HIC801W_IDLE_PAYLOAD)])
        client = _client(statuses=lambda mid: next(reads))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[-1]["stop_succeeded"] is True

    @pytest.mark.asyncio
    async def test_cloud_success_alone_never_confirms_an_encoding(self):
        """The whole safeguard: the cloud accepts every call and nothing is confirmed.

        A probe that trusted the response would report the first candidate as
        the answer here and send a wrong encoding into the implementation.
        """
        client = _client()
        client.control_work_mode.return_value = "success"
        client.control_work_mode_dp.return_value = "success"

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert all(a["cloud_state"] == "success" for a in run.attempts)
        assert run.confirmed_label is None

    @pytest.mark.asyncio
    async def test_a_rejected_call_is_recorded_and_the_walk_carries_on(self):
        """One endpoint refusing is evidence about that endpoint, not a reason to stop."""

        def send(**kwargs):
            raise RainPointApiError("controlWorkMode failed: code 3")

        client = _client()
        client.control_work_mode = AsyncMock(side_effect=send)

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        rejected = [a for a in run.attempts if a["outcome"] == OUTCOME_REJECTED]
        assert rejected
        assert "code 3" in rejected[0]["error"]
        assert len(run.attempts) == len(station_candidates())

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_not_scored_as_the_device_declining(self):
        """A timeout says nothing about the encoding, so it is labelled as transport."""
        client = _client()
        client.control_work_mode = AsyncMock(side_effect=TimeoutError())

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["outcome"] == OUTCOME_REJECTED
        assert run.attempts[0]["error"] == "transport: TimeoutError"

    @pytest.mark.asyncio
    async def test_an_unreadable_read_back_is_its_own_outcome(self):
        """Never scored as a miss: a decode failure is not evidence the command did nothing."""
        client = _client(statuses=lambda mid: {"subDeviceStatus": [{"id": "D1", "value": "10#notavalidframe"}]})

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert {a["outcome"] for a in run.attempts} == {OUTCOME_UNREADABLE}
        assert run.confirmed_label is None

    @pytest.mark.asyncio
    async def test_a_failed_status_call_reads_as_unreadable_rather_than_raising(self):
        client = _client()
        client.get_device_status = AsyncMock(side_effect=RainPointApiError("getDeviceStatus failed: code 9"))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert {a["outcome"] for a in run.attempts} == {OUTCOME_UNREADABLE}

    @pytest.mark.asyncio
    async def test_the_recorded_request_carries_no_account_identifiers(self):
        """This record is written to be attached to a public issue.

        deviceName and productKey are sent on the wire but must never reach the
        record, since the diagnostics dump redacts them by name everywhere else
        and a value that never enters the payload cannot be missed by a
        redaction list that changes later.
        """
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        blob = repr(run.as_dict())
        assert "hub-device-name" not in blob
        assert "hub-product-key" not in blob

    @pytest.mark.asyncio
    async def test_the_probe_targets_the_station_the_constant_names(self):
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.station == HIC_PROBE_STATION
        assert run.attempts[0]["request"]["port"] == HIC_PROBE_STATION


class TestRainDelayWalk:
    """The harmless stage, which has no read-back to confirm against."""

    @pytest.mark.asyncio
    async def test_every_delay_candidate_is_tried(self):
        """There is nothing to stop on, and each attempt waters nothing."""
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert len(run.attempts) == len(rain_delay_candidates())
        assert run.confirmed_label is None

    @pytest.mark.asyncio
    async def test_the_delay_stage_never_claims_a_confirmation_it_cannot_make(self):
        """Variant 279 declares no STA_ counterpart, so the record says so outright.

        The frames are recorded now, which is emphatically not the same as
        scoring them: nothing in a status frame is yet known to carry a delay,
        so every attempt still comes back NO_EFFECT and the word on the record
        says the reading was kept rather than judged.
        """
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert {a["read_back"] for a in run.attempts} == {"recorded_not_scored"}
        assert {a["outcome"] for a in run.attempts} == {OUTCOME_NO_EFFECT}
        assert run.confirmed_label is None

    @pytest.mark.asyncio
    async def test_the_delay_stage_reads_a_baseline_before_it_sends_anything(self):
        """The comparison needs a starting point from this unit, in this session.

        Every committed capture was taken with no delay set, so none of them
        can show which field a delay moves. The frame taken before the first
        candidate is the only same-unit reading that predates every write this
        stage makes.
        """
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert run.baseline_frame == SAMPLE_HIC801W_IDLE_PAYLOAD
        # One baseline plus one read-back per candidate.
        assert client.get_device_status.await_count == len(rain_delay_candidates()) + 1

    @pytest.mark.asyncio
    async def test_the_station_stage_takes_no_baseline(self):
        """It has a read-back it can actually score, so a baseline would buy nothing."""
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.baseline_frame is None

    @pytest.mark.asyncio
    async def test_the_delay_stage_never_sends_a_station_command(self):
        """A stage sold as harmless must not touch the watering datapoint."""
        client = _client()

        await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        sent_codes = {call.kwargs.get("dp_code") for call in client.control_work_mode_dp.await_args_list}
        assert DP_CODE_CTL_WATER not in sent_codes


class TestProbeRunRecord:
    """The shape the diagnostics dump carries."""

    def test_as_dict_counts_its_own_attempts(self):
        run = ProbeRun(kind="station", station=3)
        run.attempts.append({"label": "x"})

        dumped = run.as_dict()

        assert dumped["attempt_count"] == 1
        assert dumped["kind"] == "station"

    def test_stop_candidate_omits_a_duration_the_original_never_carried(self):
        """A datapoint candidate sends no duration field, so its stop must not invent one."""
        original = ProbeCandidate(label="dp", endpoint=ENDPOINT_WORK_MODE_DP, port=0, mode=1, dp_code=7, param="0301")

        stop = _stop_candidate(original, 3)

        assert stop.duration is None
        assert stop.mode == 0

    def test_stop_candidate_zeroes_a_duration_the_original_did_carry(self):
        """controlWorkMode wants the field present on a close even though it is ignored."""
        original = ProbeCandidate(label="wm", endpoint=ENDPOINT_WORK_MODE, port=3, mode=1, duration=60)

        stop = _stop_candidate(original, 3)

        assert stop.duration == 0


class TestWhatOneAttemptRecords:
    """The two things added after two support round trips came back empty."""

    @pytest.mark.asyncio
    async def test_every_read_back_keeps_the_frame_it_read(self):
        """The frame carries STA_TS_DET, which the decoder deliberately never reads.

        That field is what latched the station number on the one real unit that
        has run this, and it survives a controller giving up on a station with
        no solenoid answering, which the current_station read-back does not.
        """
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["frame_after"] == PROBED_STATION_PAYLOAD

    @pytest.mark.asyncio
    async def test_a_read_back_that_failed_records_no_frame_rather_than_a_stale_one(self):
        client = _client(statuses=lambda mid: None)

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["outcome"] == OUTCOME_UNREADABLE
        assert run.attempts[0]["frame_after"] is None

    @pytest.mark.asyncio
    async def test_the_delay_stage_keeps_the_frame_after_every_candidate(self):
        """The evidence is preserved now even though nothing reads it yet.

        Which field a rain delay moves can be established separately, by an
        owner setting one in the vendor app and sending the frame that results.
        These recordings are what turn that answer into the identity of the
        candidate that set it, instead of another round trip asking for one
        press per guess.
        """
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert len(run.attempts) == len(rain_delay_candidates())
        assert all(a["frame_after"] == SAMPLE_HIC801W_IDLE_PAYLOAD for a in run.attempts)

    @pytest.mark.asyncio
    async def test_each_attempt_is_announced_where_a_plain_log_download_finds_it(self, caplog):
        """At INFO the first real run came back with a log carrying nothing.

        A default install records WARNING and above, so the level is the whole
        difference between an owner's log answering this and not.
        """
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.control_probe"):
            await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(station_candidates()[0].label in line and OUTCOME_CONFIRMED in line for line in lines)

    @pytest.mark.asyncio
    async def test_an_announced_attempt_carries_no_frame_and_no_addressing_field(self, caplog):
        """The log line stays inside the rule the cloud-record paths already follow."""
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.control_probe"):
            await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert PROBED_STATION_PAYLOAD not in text
        assert SENSOR_INFO["device_name"] not in text
        assert SENSOR_INFO["product_key"] not in text
        assert str(SENSOR_INFO["mid"]) not in text


class TestStatusReadBackGuards:
    """The read-back walks a cloud-supplied list, so its shape is never assumed.

    Every branch here answers None, and None is scored as "unreadable" rather
    than as "the station did not start". That distinction is the whole reason
    these guards return rather than fall through to a default of 0.
    """

    @pytest.mark.parametrize(
        ("status", "why"),
        [
            (None, "response body absent entirely"),
            ("not-a-dict", "response body not a mapping"),
            ({}, "no subDeviceStatus key"),
            ({"subDeviceStatus": None}, "subDeviceStatus present but null"),
            ({"subDeviceStatus": []}, "no readings in the list"),
            ({"subDeviceStatus": ["not-a-dict"]}, "list member not a mapping"),
            ({"subDeviceStatus": [{"value": "10#ab"}]}, "entry carries no id"),
            ({"subDeviceStatus": [{"id": 7, "value": "10#ab"}]}, "id is not a string"),
            ({"subDeviceStatus": [{"id": "D2", "value": "10#ab"}]}, "only another address reported"),
            ({"subDeviceStatus": [{"id": "D1", "value": None}]}, "reading present but null"),
            ({"subDeviceStatus": [{"id": "D1", "value": 1234}]}, "reading is not a string"),
        ],
    )
    def test_a_malformed_status_response_reads_as_unknown(self, status, why):
        assert _state_from_status(status, 1)["station"] is None, why

    def test_a_well_formed_response_reads_the_station(self):
        """The positive case, so the guards above cannot pass by rejecting everything."""
        status = {"subDeviceStatus": [{"id": "D1", "value": PROBED_STATION_PAYLOAD}]}

        assert _state_from_status(status, 1)["station"] == HIC_PROBE_STATION

    def test_the_reading_for_the_addressed_sub_device_is_the_one_read(self):
        """A hub carrying several children must not have another one's frame read."""
        status = {
            "subDeviceStatus": [
                {"id": "D2", "value": SAMPLE_HIC801W_IDLE_PAYLOAD},
                {"id": "D1", "value": PROBED_STATION_PAYLOAD},
            ]
        }

        assert _state_from_status(status, 1)["station"] == HIC_PROBE_STATION
        assert _state_from_status(status, 2)["station"] == 0


class TestTheSubDeviceIdIsResolvedNumerically:
    """The defect that made a working walk record no winner at all.

    The first real unit to run this probe reports its sub-device as ``D01``.
    The read-back matched ids by rebuilding ``f"D{addr}"``, so it compared
    against ``D1``, matched nothing, and scored all ten attempts unreadable
    while the commands were in fact starting the station. Every test here fails
    against that string comparison and passes against the numeric one.
    """

    def test_a_zero_padded_id_resolves_to_the_same_addr(self):
        state = _state_from_status(_status(PROBED_STATION_PAYLOAD, addr="01"), 1)

        assert state["station"] == HIC_PROBE_STATION
        assert state["frame"] == PROBED_STATION_PAYLOAD

    def test_an_unpadded_id_still_resolves(self):
        """The fix must not trade one spelling for the other."""
        state = _state_from_status(_status(PROBED_STATION_PAYLOAD, addr="1"), 1)

        assert state["station"] == HIC_PROBE_STATION

    def test_a_different_addr_is_still_not_this_device(self):
        """Numeric resolution must not turn into matching everything."""
        state = _state_from_status(_status(PROBED_STATION_PAYLOAD, addr="02"), 1)

        assert state["station"] is None
        assert state["frame"] is None

    def test_a_non_numeric_id_is_skipped_rather_than_raising(self):
        state = _state_from_status({"subDeviceStatus": [{"id": "connected", "value": "1"}]}, 1)

        assert state["station"] is None

    @pytest.mark.asyncio
    async def test_a_walk_against_a_zero_padded_unit_confirms(self):
        """The end-to-end shape of the defect, not just the helper it lived in.

        Asserting on _state_from_status alone would have kept passing for the
        wrong reason if the walk stopped calling it, so this drives the real
        entry point against the id spelling the real unit sends.
        """
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD, addr="01"))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label == "work_mode_port"
        assert run.attempts[0]["confirmed_by"] == "read_back"


class TestTheCommandResponseIsAWitnessToo:
    """Why the read-back alone is not enough on this hardware.

    Both control endpoints answer with the controller's own state frame, and on
    the first real run that frame already showed the commanded station running.
    The read-back that follows the settle can miss it: this controller drops a
    station whose solenoid does not answer within seconds, which is precisely
    the state a probe is run in. Confirming on either frame is what keeps a
    working encoding from scoring as a miss.
    """

    @pytest.mark.asyncio
    async def test_a_response_frame_confirms_when_the_read_back_has_gone_quiet(self):
        client = _client(
            send=lambda **kw: PROBED_STATION_PAYLOAD,
            statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD),
        )

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label == "work_mode_port"
        assert run.attempts[0]["confirmed_by"] == "response"
        assert run.attempts[0]["station_in_response"] == HIC_PROBE_STATION

    @pytest.mark.asyncio
    async def test_the_read_back_is_preferred_when_both_witnesses_speak(self):
        """Not a behavioural nicety: it records which evidence the verdict rests on."""
        client = _client(
            send=lambda **kw: PROBED_STATION_PAYLOAD,
            statuses=lambda mid: _status(PROBED_STATION_PAYLOAD),
        )

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["confirmed_by"] == "read_back"

    @pytest.mark.asyncio
    async def test_a_bare_acknowledgement_confirms_nothing(self):
        """The response is a witness only when it is actually a state frame.

        The endpoints can answer with a plain word, and treating that as
        evidence would make every candidate confirm and the walk meaningless.
        """
        client = _client(send=lambda **kw: "ok", statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label is None
        assert {a["station_in_response"] for a in run.attempts} == {None}

    @pytest.mark.asyncio
    async def test_a_bare_acknowledgement_never_reaches_the_decoder(self):
        """decode_hic801w logs an exception on an unparseable blob.

        Handing it every response would put a traceback in the owner's log for
        each attempt, in a run whose whole purpose is to produce a readable log.
        """
        client = _client(send=lambda **kw: "ok", statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD))

        with patch("custom_components.rainpoint.control_probe.decode_hic801w", wraps=decode_hic801w) as decoder:
            await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert all(call.args[0].startswith("10#") for call in decoder.call_args_list)


class TestTheRunLengthTheControllerReports:
    """The seconds-or-minutes question, carried in the record rather than inferred.

    The first real run asked for 60 and the controller reported 3600. Until
    that is settled no duration can reach a user-facing control, so the number
    asked for and the number reported both have to survive into the report.
    """

    @pytest.mark.asyncio
    async def test_the_reported_run_length_is_recorded_next_to_the_one_asked_for(self):
        client = _client(
            send=lambda **kw: SAMPLE_HIC801W_PROBE_RESPONSE_STATION3,
            statuses=lambda mid: _status(SAMPLE_HIC801W_IDLE_PAYLOAD),
        )

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["run_seconds_in_response"] == 3600
        assert run.attempts[0]["request"]["duration"] == HIC_PROBE_RUN_VALUE

    @pytest.mark.asyncio
    async def test_the_read_back_records_its_run_length_too(self):
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["run_seconds_after"] == 60

    def test_the_value_sent_separates_all_three_readings_in_play(self):
        """Seconds, minutes, and the field being ignored must not collide.

        3600 is what the controller reported the one time this was tried, for a
        command that asked for 60. A value whose minutes reading landed back on
        3600 could not tell "read as minutes" from "ignored, ran its own
        default", and the owner would have pressed the button for nothing.
        """
        readings = {
            HIC_PROBE_RUN_VALUE,  # read as seconds
            HIC_PROBE_RUN_VALUE * 60,  # read as minutes
            3600,  # ignored, the runtime observed on a real unit
        }

        assert len(readings) == 3


class TestTheStopIsNotCreditedWithoutEvidence:
    """An unreadable frame was counted as a successful stop.

    That is the one scoring rule that could report a stop command as working
    with no frame supporting it, and it sat directly behind a read-back that
    was matching nothing at all.
    """

    @pytest.mark.asyncio
    async def test_a_stop_nobody_could_read_is_not_a_success(self):
        statuses = iter(
            [
                _status(PROBED_STATION_PAYLOAD, addr="01"),  # the start confirms
                {"subDeviceStatus": []},  # the stop read-back says nothing
            ]
        )
        client = _client(statuses=lambda mid: next(statuses))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.confirmed_label == "work_mode_port"
        assert run.attempts[-1]["stop_succeeded"] is False

    @pytest.mark.asyncio
    async def test_a_station_reported_off_is_a_success(self):
        statuses = iter(
            [
                _status(PROBED_STATION_PAYLOAD, addr="01"),
                _status(SAMPLE_HIC801W_IDLE_PAYLOAD, addr="01"),
            ]
        )
        client = _client(statuses=lambda mid: next(statuses))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[-1]["stop_succeeded"] is True


# Constructed, not captured, and deliberately kept out of payload_samples for
# that reason. No real unit has ever been recorded mid-run with a run length
# short enough to have expired before the probe's own settle, because nobody
# has asked one for a run that short until now. The station 1 capture with its
# STA_DURATION rewritten from 60 seconds to 2 is the smallest honest way to
# exercise the rule that exists precisely for that case.
_ALREADY_EXPIRED_RUN_PAYLOAD = PROBED_STATION_PAYLOAD.replace("3C000000", "02000000")


class TestAStopIsOnlyCreditedWhenItCouldHaveProvedAnything:
    """The confound that sent the probe to a wired station.

    Station 3 on the reporter's unit has no solenoid, and this hardware drops a
    station whose solenoid does not answer within seconds. That is the same few
    seconds the probe waits before sending its stop, so the station reading off
    afterwards was going to happen whether or not the stop worked. Reporting
    that as a successful stop would have been a fabricated answer, and it would
    have read exactly like a real one.
    """

    def test_a_run_that_had_already_expired_proves_nothing(self):
        assert _stop_would_prove_anything({"run_seconds_after": HIC_PROBE_SETTLE_SECONDS}) is False

    def test_a_run_still_going_when_the_stop_went_out_can_be_credited(self):
        assert _stop_would_prove_anything({"run_seconds_after": HIC_PROBE_SETTLE_SECONDS + 1}) is True

    def test_an_unreadable_run_length_is_not_treated_as_proof(self):
        """Absence of evidence, so False rather than the benefit of the doubt."""
        assert _stop_would_prove_anything({}) is False
        assert _stop_would_prove_anything({"run_seconds_after": None, "run_seconds_in_response": None}) is False

    def test_the_response_run_length_is_used_when_the_read_back_had_none(self):
        assert _stop_would_prove_anything({"run_seconds_after": None, "run_seconds_in_response": 60}) is True

    @pytest.mark.asyncio
    async def test_a_station_that_dropped_itself_is_not_a_working_stop(self):
        """End to end, because the helper passing alone would not have saved us.

        The whole defect was a scoring rule that read plausibly in isolation and
        was wrong about the hardware it ran against.
        """
        reads = iter([_status(_ALREADY_EXPIRED_RUN_PAYLOAD), _status(SAMPLE_HIC801W_IDLE_PAYLOAD)])
        client = _client(statuses=lambda mid: next(reads))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        stop = run.attempts[-1]
        assert run.confirmed_label == "work_mode_port"
        assert stop["station_after"] == 0
        assert stop["stop_conclusive"] is False
        assert stop["stop_succeeded"] is False

    @pytest.mark.asyncio
    async def test_a_wired_station_going_quiet_is_a_working_stop(self):
        """The case the move to station 1 exists to make reachable."""
        reads = iter([_status(PROBED_STATION_PAYLOAD), _status(SAMPLE_HIC801W_IDLE_PAYLOAD)])
        client = _client(statuses=lambda mid: next(reads))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        stop = run.attempts[-1]
        assert stop["stop_conclusive"] is True
        assert stop["stop_succeeded"] is True

    @pytest.mark.asyncio
    async def test_a_station_still_running_after_the_stop_is_not_credited(self):
        """Conclusive and failed is a real result, and a different one from unprovable."""
        client = _client(statuses=lambda mid: _status(PROBED_STATION_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        stop = run.attempts[-1]
        assert stop["stop_conclusive"] is True
        assert stop["stop_succeeded"] is False
