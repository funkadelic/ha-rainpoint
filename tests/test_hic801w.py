"""End-to-end HIC801W timeline: a real coordinator poll through the real
sensor platform, proving HIC-01 and D-09/HIC-03 on a live entity object
rather than on a constructed envelope or an injected coordinator.data
snapshot.

This behaviour spans the decoder, the coordinator's registry dispatch and
unknown-model notification, and the sensor platform's factory and entity
lifecycle, matching the tests/test_hub_identity.py and
tests/test_orphan_removal.py precedent of a feature-scoped module rather
than living inside tests/test_sensor.py. Plan 30-04 extends this file with
the whole-entity-set proof once the run-timing, program-list and per-station
binary sensors exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import coordinator as coordinator_module
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, MODEL_HIC801W
from custom_components.rainpoint.coordinator import RainPointCoordinator
from custom_components.rainpoint.sensor import RainPointHicCurrentStationSensor, async_setup_entry
from tests.payload_samples import SAMPLE_HIC801W_IDLE_PAYLOAD, SAMPLE_HIC801W_STATION3_PAYLOAD

_HUB_MID = 200
_SUB_ADDR = 1
_SENSOR_KEY = f"100_{_HUB_MID}_{_SUB_ADDR}"

# The station-3 capture's STA_WATER_ZONES b3 mutated from 00 to a non-zero
# byte, so the shape check rejects it (D-10) without needing a second real
# capture.
_B3_MUTATED_PAYLOAD = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F703FF0301F9")
assert _B3_MUTATED_PAYLOAD != SAMPLE_HIC801W_STATION3_PAYLOAD


def _hub_devices():
    """One home, one hub, one HIC801W sub-device at addr 1, modelCode 279."""
    return [
        {
            "mid": _HUB_MID,
            "name": "Hub A",
            "deviceName": "d",
            "productKey": "pk",
            "homeName": "H",
            "subDevices": [
                {
                    "addr": _SUB_ADDR,
                    "name": "Irrigation Controller",
                    "model": MODEL_HIC801W,
                    "modelCode": 279,
                    "softVer": "1.1.1026",
                }
            ],
        }
    ]


def _status(payload: str):
    """One subDeviceStatus entry carrying the given raw HIC801W payload."""
    return [
        {
            "mid": _HUB_MID,
            "subDeviceStatus": [{"id": "D01", "value": payload, "time": 1785420002247}],
        }
    ]


async def _build_hic801w_timeline():
    """Drive construct -> first refresh -> platform setup with the station-3
    capture, patching persistent_notification.async_create for the whole
    sequence so HIC-01 (no notification fires) is asserted rather than
    assumed.

    Returns (coordinator, client, entity, mock_notify).
    """
    client = AsyncMock()
    client.get_devices_by_hid.return_value = _hub_devices()
    client.get_multiple_device_status.return_value = _status(SAMPLE_HIC801W_STATION3_PAYLOAD)

    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_HIDS: [100]}
    entry.options = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": {}}}

    coordinator = RainPointCoordinator(hass, client, entry)
    hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

    with patch.object(coordinator_module, "async_create") as mock_notify:
        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

    stations = [e for e in captured if isinstance(e, RainPointHicCurrentStationSensor)]
    assert len(stations) == 1
    return coordinator, client, stations[0], mock_notify


class TestHic801wRealTimeline:
    """Construct -> first refresh -> platform setup -> refresh, driven on a
    real coordinator and real entity object throughout."""

    @pytest.mark.asyncio
    async def test_registration_stops_the_unknown_model_notification(self):
        """HIC-01, proven on the async_create seam coordinator.py imports,
        not inferred from registry membership alone."""
        coordinator, _client, _entity, mock_notify = await _build_hic801w_timeline()

        assert coordinator.data["sensors"][_SENSOR_KEY]["data"]["type"] == "irrigation_controller"
        assert coordinator.data["sensors"][_SENSOR_KEY]["data"]["type"] != "unknown"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_station_3_capture_reads_3_and_stays_available(self):
        """The real platform, off a real first refresh, builds one Current
        Station sensor reading the station-3 capture's settled value."""
        _coordinator, _client, entity, _mock_notify = await _build_hic801w_timeline()

        assert entity.native_value == "3"
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_idle_refresh_moves_the_same_entity_object_to_none(self):
        """Swapping the polled payload to the idle capture and refreshing
        moves the same entity object to "none" -- no reload, no second
        setup."""
        coordinator, client, entity, _mock_notify = await _build_hic801w_timeline()

        client.get_multiple_device_status.return_value = _status(SAMPLE_HIC801W_IDLE_PAYLOAD)
        await coordinator.async_refresh()

        assert entity.native_value == "none"
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_rejected_frame_yields_no_state_but_stays_available(self):
        """D-09 / HIC-03 on a live object: a b3-mutated frame decodes to the
        error envelope, the same entity object reads no state, and it stays
        available because the device is reachable and still polling -- it is
        the payload that did not parse."""
        coordinator, client, entity, _mock_notify = await _build_hic801w_timeline()

        client.get_multiple_device_status.return_value = _status(_B3_MUTATED_PAYLOAD)
        await coordinator.async_refresh()

        assert entity.native_value is None
        assert entity.available is True
