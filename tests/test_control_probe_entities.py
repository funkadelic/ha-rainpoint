"""Tests for the probe buttons and the gate that keeps them off ordinary accounts.

The gate is the load the tests here carry. This surface writes commands to an
irrigation controller, so "no ordinary user can reach it" has to be a property
the suite proves rather than a claim in a docstring.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.button import async_setup_entry
from custom_components.rainpoint.const import (
    CONF_HIC_CONTROL_PROBE_ENABLED,
    DOMAIN,
    HIC_PROBE_STATION,
    MODEL_HIC801W,
)
from custom_components.rainpoint.control_probe_entities import (
    KIND_RAIN_DELAY,
    KIND_STATION,
    PROBE_RESULT_STORE_KEY,
    RainPointProbeRainDelayButton,
    RainPointProbeStationButton,
    probe_results,
    store_probe_result,
)
from tests.helpers import make_coordinator_data, make_sensor_entry
from tests.payload_samples import SAMPLE_HIC801W_IDLE_PAYLOAD

SENSOR_KEY = "100_85577_1"


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """Collapse the inter-attempt settle so a button press does not sleep the suite."""
    monkeypatch.setattr("custom_components.rainpoint.control_probe.HIC_PROBE_SETTLE_SECONDS", 0)


def _hub():
    return {"hid": 100, "mid": 85577, "name": "Hub", "model": "HIC801W", "deviceName": "dn", "productKey": "pk"}


def _setup(model=MODEL_HIC801W, probe_enabled=True, hubs=None):
    """Return (hass, entry, coordinator) wired the way button.py expects."""
    sensor_info = make_sensor_entry(
        hid=100, mid=85577, addr=1, model=model, sub_name="Controller", data={"type": "irrigation_controller"}
    )
    coordinator = MagicMock()
    coordinator.data = make_coordinator_data(hubs=hubs if hubs is not None else [_hub()], sensors={SENSOR_KEY: sensor_info})
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.options = {CONF_HIC_CONTROL_PROBE_ENABLED: probe_enabled}
    hass = MagicMock()
    client = MagicMock()
    client.control_work_mode = AsyncMock(return_value="ok")
    client.control_work_mode_dp = AsyncMock(return_value="ok")
    client.get_device_status = AsyncMock(return_value={"subDeviceStatus": [{"id": "D1", "value": SAMPLE_HIC801W_IDLE_PAYLOAD}]})
    hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator, "client": client}}}
    return hass, entry, coordinator


async def _captured(hass, entry):
    captured = []
    await async_setup_entry(hass, entry, MagicMock(side_effect=lambda ents, **kw: captured.extend(ents)))
    return captured


class TestProbeButtonGate:
    """Who gets these buttons, and who must not."""

    @pytest.mark.asyncio
    async def test_no_probe_buttons_when_the_option_is_off(self):
        """The default path. An account that never opts in sees this surface at all."""
        hass, entry, _ = _setup(probe_enabled=False)

        captured = await _captured(hass, entry)

        assert [e for e in captured if "probe" in (e._attr_unique_id or "")] == []

    @pytest.mark.asyncio
    async def test_no_probe_buttons_on_a_model_without_the_single_datapoint_shape(self):
        """Turning the option on must not scatter probe buttons over other hardware."""
        hass, entry, _ = _setup(model="HTV245FRF", probe_enabled=True)

        captured = await _captured(hass, entry)

        assert [e for e in captured if "probe" in (e._attr_unique_id or "")] == []

    @pytest.mark.asyncio
    async def test_both_buttons_appear_once_the_option_is_on(self):
        hass, entry, _ = _setup(probe_enabled=True)

        captured = await _captured(hass, entry)
        ids = {e._attr_unique_id for e in captured if "probe" in (e._attr_unique_id or "")}

        assert ids == {
            f"rainpoint_{SENSOR_KEY}_probe_rain_delay",
            f"rainpoint_{SENSOR_KEY}_probe_station",
        }

    @pytest.mark.asyncio
    async def test_enabling_the_probe_does_not_disturb_the_shipped_buttons(self):
        """The hub broadcast button is still built exactly once either way."""
        hass, entry, _ = _setup(probe_enabled=True)

        captured = await _captured(hass, entry)
        broadcast = [e for e in captured if "broadcast_now" in (e._attr_unique_id or "")]

        assert len(broadcast) == 1

    @pytest.mark.asyncio
    async def test_the_station_button_names_the_station_it_will_start(self):
        """The owner is being asked to shut the water off, so the label has to say which one."""
        hass, entry, _ = _setup(probe_enabled=True)

        captured = await _captured(hass, entry)
        station_button = next(e for e in captured if (e._attr_unique_id or "").endswith("_probe_station"))

        assert str(HIC_PROBE_STATION) in station_button._attr_name


class TestProbeButtonPress:
    """What a press actually does."""

    def _button(self, cls):
        hass, _entry, coordinator = _setup(probe_enabled=True)
        sensor_info = coordinator.data["sensors"][SENSOR_KEY]
        button = cls(coordinator, SENSOR_KEY, sensor_info, SENSOR_KEY, "entry1")
        button.hass = hass
        return button, hass

    @pytest.mark.asyncio
    async def test_pressing_the_delay_button_records_a_run(self):
        button, hass = self._button(RainPointProbeRainDelayButton)

        await button.async_press()

        recorded = probe_results(hass, "entry1")
        assert recorded[KIND_RAIN_DELAY]["kind"] == KIND_RAIN_DELAY
        assert recorded[KIND_RAIN_DELAY]["attempt_count"] > 0

    @pytest.mark.asyncio
    async def test_each_stage_records_under_its_own_slot(self):
        """Running the station walk must not overwrite a delay result not yet reported."""
        hass, _entry, coordinator = _setup(probe_enabled=True)
        sensor_info = coordinator.data["sensors"][SENSOR_KEY]
        delay = RainPointProbeRainDelayButton(coordinator, SENSOR_KEY, sensor_info, SENSOR_KEY, "entry1")
        station = RainPointProbeStationButton(coordinator, SENSOR_KEY, sensor_info, SENSOR_KEY, "entry1")
        delay.hass = station.hass = hass

        await delay.async_press()
        await station.async_press()

        recorded = probe_results(hass, "entry1")
        assert set(recorded) == {KIND_RAIN_DELAY, KIND_STATION}
        assert recorded[KIND_RAIN_DELAY]["kind"] == KIND_RAIN_DELAY
        assert recorded[KIND_STATION]["kind"] == KIND_STATION

    @pytest.mark.asyncio
    async def test_a_press_stamps_when_it_finished(self):
        button, hass = self._button(RainPointProbeRainDelayButton)

        await button.async_press()

        assert probe_results(hass, "entry1")[KIND_RAIN_DELAY]["finished_at"] is not None

    @pytest.mark.asyncio
    async def test_the_press_reads_the_live_record_rather_than_its_build_snapshot(self):
        """This is a write, so an identity that moved under the entity must not be used."""
        button, hass = self._button(RainPointProbeStationButton)
        button.coordinator.data["sensors"][SENSOR_KEY]["mid"] = 999999

        await button.async_press()

        client = hass.data[DOMAIN]["entry1"]["client"]
        assert client.control_work_mode.await_args.kwargs["mid"] == 999999

    @pytest.mark.asyncio
    async def test_the_button_stays_available_on_a_device_whose_frame_did_not_decode(self):
        """The base rule reads availability off a decoded payload.

        That is right for a reading and wrong here: an undecodable frame is
        exactly when an owner is most likely to have been asked to press this.
        """
        button, _ = self._button(RainPointProbeStationButton)
        button.coordinator.data["sensors"][SENSOR_KEY]["data"] = None

        assert button.available is True


class TestProbeResultStore:
    """The record's own storage, which the diagnostics dump reads."""

    def test_results_are_empty_before_anything_is_run(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": {}}}

        assert probe_results(hass, "entry1") == {}

    def test_results_are_empty_for_an_entry_that_does_not_exist(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {}}

        assert probe_results(hass, "missing") == {}

    def test_storing_against_a_missing_entry_store_does_not_raise(self):
        """A lost record is a better outcome than taking the press down with it."""
        hass = MagicMock()
        hass.data = {DOMAIN: {}}

        store_probe_result(hass, "missing", KIND_STATION, {"kind": KIND_STATION})

        assert probe_results(hass, "missing") == {}

    def test_a_second_run_of_one_stage_replaces_only_that_stage(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": {}}}

        store_probe_result(hass, "entry1", KIND_RAIN_DELAY, {"n": 1})
        store_probe_result(hass, "entry1", KIND_STATION, {"n": 2})
        store_probe_result(hass, "entry1", KIND_STATION, {"n": 3})

        assert probe_results(hass, "entry1") == {KIND_RAIN_DELAY: {"n": 1}, KIND_STATION: {"n": 3}}

    def test_the_store_key_is_where_diagnostics_looks(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": {}}}

        store_probe_result(hass, "entry1", KIND_STATION, {"n": 1})

        assert hass.data[DOMAIN]["entry1"][PROBE_RESULT_STORE_KEY] == {KIND_STATION: {"n": 1}}
