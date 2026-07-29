"""Tests for the shared sub-device entity plumbing (entity.py)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.rainpoint.entity import sub_device_attributes


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
