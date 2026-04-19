"""Shared test utility functions for RainPoint integration tests.

``make_sensor_coordinator`` is the shared coordinator builder used by the entity
test modules (sensor, valve, number, diagnostic_sensors) so they do not each
rehydrate the canonical ``{"hubs", "status", "sensors"}`` shape inline.
"""

_SENTINEL = object()


def make_sensor_entry(
    hid=100,
    mid=200,
    addr=1,
    model="HCS026FRF",
    sub_name="Test Sensor",
    data=None,
):
    """Return a dict matching the coordinator 'sensors' entry shape."""
    return {
        "hid": hid,
        "mid": mid,
        "addr": addr,
        "home_name": "Test Home",
        "hub_name": "Test Hub",
        "sub_name": sub_name,
        "model": model,
        "firmware_version": "1.0.0",
        "device_name": "test-device",
        "product_key": "pk-test",
        "raw_status": {"value": "test", "time": 1700000000000},
        "data": data,
    }


def make_coordinator_data(hubs=None, sensors=None, status=None):
    """Return a coordinator data dict with the standard shape."""
    return {
        "hubs": hubs if hubs is not None else [],
        "status": status if status is not None else {},
        "sensors": sensors if sensors is not None else {},
    }


def make_hub_info(
    hid=100,
    name="Test Hub",
    model="HTV0540FRF",
    mac="AA:BB:CC:DD:EE:FF",
    softVer="2.0.0",
):
    """Return a hub dict matching coordinator hub shape."""
    return {
        "hid": hid,
        "name": name,
        "model": model,
        "mac": mac,
        "softVer": softVer,
    }


def make_sensor_coordinator(
    model: str = "HCS026FRF",
    data=_SENTINEL,
    hid: int = 100,
    mid: int = 200,
    addr: int = 1,
    sub_name: str = "Test Sensor",
    firmware_version: str = "1.0.0",
    sensor_key: str | None = None,
    hubs=None,
    status=None,
    extra_sensor_info: dict | None = None,
):
    """Build a MagicMock coordinator with the canonical data shape for entity tests.

    The returned object has ``.data == {"hubs", "status", "sensors"}`` where the
    ``"sensors"`` dict contains exactly one entry keyed by ``f"{hid}_{mid}_{addr}"``
    (or by ``sensor_key`` if supplied). Pass ``data=None`` explicitly to simulate
    the "decoder ran but produced nothing" path; omit ``data`` to get ``{}``.
    """
    from unittest.mock import MagicMock

    key = sensor_key if sensor_key is not None else f"{hid}_{mid}_{addr}"
    entry = make_sensor_entry(
        hid=hid,
        mid=mid,
        addr=addr,
        model=model,
        sub_name=sub_name,
        data={} if data is _SENTINEL else data,
    )
    entry["firmware_version"] = firmware_version
    if extra_sensor_info:
        entry.update(extra_sensor_info)

    coord_data = make_coordinator_data(
        hubs=hubs,
        sensors={key: entry},
        status=status,
    )

    coord = MagicMock()
    coord.data = coord_data
    return coord
