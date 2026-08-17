"""Tests for the opt-in HIC control-encoding probe.

Two properties matter more than line coverage here, because this module writes
commands to hardware the maintainer does not own:

- Nothing is ever scored as working on the cloud's say-so. A candidate is
  confirmed only when a status frame read back afterwards reports the station,
  and every other path has its own outcome word.
- The walk stops the moment something works, and sends the stop.

Both are asserted directly rather than inferred from a call count.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.const import (
    HIC_PROBE_RUN_SECONDS,
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
    _u16,
    async_run_probe,
    rain_delay_candidates,
    station_candidates,
)
from tests.payload_samples import (
    SAMPLE_HIC801W_IDLE_PAYLOAD,
    SAMPLE_HIC801W_STATION3_PAYLOAD,
)

SENSOR_INFO = {
    "mid": 85577,
    "addr": 1,
    "device_name": "hub-device-name",
    "product_key": "hub-product-key",
    "model": "HIC801W",
}


def _status(payload: str, addr: int = 1) -> dict:
    """Return a getDeviceStatus body carrying one sub-device reading."""
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
    """Collapse the inter-attempt settle so the suite does not sleep through it."""
    monkeypatch.setattr("custom_components.rainpoint.control_probe.HIC_PROBE_SETTLE_SECONDS", 0)


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

    def test_every_station_candidate_is_distinguishable(self):
        """No two candidates send the same thing, which is why station 3 is used.

        Station 1 would collapse the station-number and bitmask readings onto
        the same byte and both byte orders onto the same pair, so a hit would
        not say which encoding was right. Asserted as a property over the whole
        list rather than by spot-checking the pair that motivated it.
        """
        shapes = [(c.endpoint, c.port, c.mode, c.dp_code, c.param, c.duration, c.addr_override) for c in station_candidates()]

        assert len(set(shapes)) == len(shapes)

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

        assert durations and max(durations) <= HIC_PROBE_RUN_SECONDS

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
                return _status(SAMPLE_HIC801W_STATION3_PAYLOAD)
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
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_STATION3_PAYLOAD))

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
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_STATION3_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[-1]["stop_succeeded"] is False

    @pytest.mark.asyncio
    async def test_a_stop_the_controller_honours_is_recorded_as_succeeding(self):
        reads = iter([_status(SAMPLE_HIC801W_STATION3_PAYLOAD), _status(SAMPLE_HIC801W_IDLE_PAYLOAD)])
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
        """Variant 279 declares no STA_ counterpart, so the record says so outright."""
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert {a["read_back"] for a in run.attempts} == {"not_applicable"}
        assert {a["outcome"] for a in run.attempts} == {OUTCOME_NO_EFFECT}

    @pytest.mark.asyncio
    async def test_the_delay_stage_never_reads_the_controller_back(self):
        """No status call at all: there is nothing it could tell us."""
        client = _client()

        await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        client.get_device_status.assert_not_called()

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
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_STATION3_PAYLOAD))

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["frame_after"] == SAMPLE_HIC801W_STATION3_PAYLOAD

    @pytest.mark.asyncio
    async def test_a_read_back_that_failed_records_no_frame_rather_than_a_stale_one(self):
        client = _client(statuses=lambda mid: None)

        run = await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        assert run.attempts[0]["outcome"] == OUTCOME_UNREADABLE
        assert run.attempts[0]["frame_after"] is None

    @pytest.mark.asyncio
    async def test_the_delay_stage_records_no_frame_it_never_read(self):
        """It has no read-back at all, so a frame key there would be a fiction."""
        client = _client()

        run = await async_run_probe(client, SENSOR_INFO, kind="rain_delay", now="t0")

        assert all("frame_after" not in attempt for attempt in run.attempts)

    @pytest.mark.asyncio
    async def test_each_attempt_is_announced_where_a_plain_log_download_finds_it(self, caplog):
        """At INFO the first real run came back with a log carrying nothing.

        A default install records WARNING and above, so the level is the whole
        difference between an owner's log answering this and not.
        """
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_STATION3_PAYLOAD))

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.control_probe"):
            await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(station_candidates()[0].label in line and OUTCOME_CONFIRMED in line for line in lines)

    @pytest.mark.asyncio
    async def test_an_announced_attempt_carries_no_frame_and_no_addressing_field(self, caplog):
        """The log line stays inside the rule the cloud-record paths already follow."""
        client = _client(statuses=lambda mid: _status(SAMPLE_HIC801W_STATION3_PAYLOAD))

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.control_probe"):
            await async_run_probe(client, SENSOR_INFO, kind="station", now="t0")

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert SAMPLE_HIC801W_STATION3_PAYLOAD not in text
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
        status = {"subDeviceStatus": [{"id": "D1", "value": SAMPLE_HIC801W_STATION3_PAYLOAD}]}

        assert _state_from_status(status, 1)["station"] == HIC_PROBE_STATION

    def test_the_reading_for_the_addressed_sub_device_is_the_one_read(self):
        """A hub carrying several children must not have another one's frame read."""
        status = {
            "subDeviceStatus": [
                {"id": "D2", "value": SAMPLE_HIC801W_IDLE_PAYLOAD},
                {"id": "D1", "value": SAMPLE_HIC801W_STATION3_PAYLOAD},
            ]
        }

        assert _state_from_status(status, 1)["station"] == HIC_PROBE_STATION
        assert _state_from_status(status, 2)["station"] == 0
