"""Tests for the shared sub-device entity plumbing (entity.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.rainpoint.entity import RainPointSubDeviceEntity, sub_device_attributes


def _coordinator(entry):
    """Return a coordinator stub whose sensors map holds one entry under "k"."""
    return SimpleNamespace(data={"sensors": {"k": entry}} if entry is not None else {"sensors": {}})


class TestSubDeviceAttributes:
    """Tests for sub_device_attributes."""

    def test_firmware_and_device_timestamp(self):
        """A populated entry yields the firmware and the device timestamp trio."""
        coordinator = _coordinator(
            {
                "firmware_version": "1.4",
                "data": {
                    "device_timestamp": "2026-07-29T12:19:33+00:00",
                    "timestamp_method": "rtc",
                    "timestamp_source": "device",
                },
            }
        )
        assert sub_device_attributes(coordinator, "k") == {
            "firmware_version": "1.4",
            "device_timestamp": "2026-07-29T12:19:33+00:00",
            "timestamp_method": "rtc",
            "timestamp_source": "device",
        }

    def test_server_timestamp_fills_in_for_device_timestamp(self):
        """With only a server timestamp, it is reported as the device timestamp."""
        coordinator = _coordinator({"data": {"server_timestamp": "2026-07-29T12:00:00+00:00"}})
        attrs = sub_device_attributes(coordinator, "k")
        assert attrs["device_timestamp"] == "2026-07-29T12:00:00+00:00"
        assert attrs["timestamp_source"] == "server"
        assert "timestamp_method" not in attrs

    def test_none_reading_yields_firmware_alone(self):
        """A sub-device with no reading yet must not raise.

        The per-platform copies in valve.py and number.py fed this None
        straight into a membership test, so a device that had not reported
        raised while its attributes were being built.
        """
        coordinator = _coordinator({"firmware_version": "1.4", "data": None})
        assert sub_device_attributes(coordinator, "k") == {"firmware_version": "1.4"}

    def test_missing_entry_yields_nothing(self):
        """A sensor key the coordinator does not know yields no attributes."""
        assert sub_device_attributes(_coordinator(None), "k") == {}

    def test_coordinator_without_data_yields_nothing(self):
        """A coordinator that has not completed its first poll yields no attributes."""
        assert sub_device_attributes(SimpleNamespace(data=None), "k") == {}

    def test_absent_firmware_is_omitted(self):
        """An empty firmware string is treated as absent rather than reported."""
        coordinator = _coordinator({"firmware_version": "", "data": {}})
        assert sub_device_attributes(coordinator, "k") == {}

    def test_silent_entry_yields_firmware_alone(self):
        """A silent entry (D-09/D-11) carries neither a device nor a server
        timestamp key, so it must not raise and must yield the firmware
        attribute alone, same as a bare None reading."""
        from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE

        coordinator = _coordinator(
            {
                "firmware_version": "1.4",
                "raw_status": {},
                "data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"},
            }
        )
        assert sub_device_attributes(coordinator, "k") == {"firmware_version": "1.4"}


class TestSubDeviceEntity:
    """Tests for RainPointSubDeviceEntity."""

    @staticmethod
    def _entity(data):
        """Return an entity bound to a coordinator holding ``data``."""
        coordinator = MagicMock()
        coordinator.data = data
        return RainPointSubDeviceEntity(coordinator, "k", {"addr": 1}, "slug")

    def test_sensor_data_without_coordinator_data(self):
        """A coordinator with no data yet reads as no reading, not a crash.

        Matches the guard sub_device_attributes already applies, so both halves
        of this module agree on what an empty coordinator means.
        """
        entity = self._entity(None)
        assert entity._sensor_data is None
        assert entity.available is False

    def test_sensor_data_reads_through_to_the_entry(self):
        """A populated entry yields its decoded reading and reads as available."""
        entity = self._entity({"sensors": {"k": {"data": {"type": "valve"}}}})
        assert entity._sensor_data == {"type": "valve"}
        assert entity.available is True

    def test_silent_entry_reads_as_unavailable(self):
        """A silent entry's data is truthy, but must still read as unavailable:
        a battery/RSSI/generic entity bound to this key must not look wired up
        while reading nothing (D-02/D-12)."""
        from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE

        entity = self._entity({"sensors": {"k": {"data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"}}}})
        assert entity.available is False
