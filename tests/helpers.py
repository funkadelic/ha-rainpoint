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
    mid=1001,
):
    """Return a hub dict matching coordinator hub shape."""
    return {
        "hid": hid,
        "mid": mid,
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


def make_silent_wrapper_hub_record(mid=200, addr=1, model="HTV210B", sub_name="BT Valve"):
    """Return the cloud's Bluetooth wrapper record carrying one silent child.

    Every identity field is the empty string rather than absent, which is what
    the cloud actually returns and what makes is_hub_record answer False. Two
    test modules drive timelines off this shape (entity creation in
    test_sensor.py, the parenting reconcile in test_init.py), so it lives here
    rather than in whichever module happened to need it first.
    """
    return {
        "mid": mid,
        "homeName": "Home",
        "name": "",
        "deviceName": "",
        "productKey": "",
        "model": "",
        "subDevices": [{"addr": addr, "model": model, "name": sub_name, "softVer": "1.0"}],
    }


VALVE_ZONES_TLV_PAYLOAD = "11#17E1D70018DC0119D8001AD8001D201E20"
"""A captured HTV245FRF TLV status value reporting four zones.

Shared because two modules drive real coordinator timelines off it: the valve
platform's late-add path (test_valve.py) and the sub-device parenting timeline
(test_device.py). Neither should own it, and neither should reach into the
other's test class to borrow it.
"""


def make_valve_zone_status(mid=20, sid="D01", zones_reported=True, time_ms=1785420002247):
    """Return a multipleDeviceStatus list for one valve hub child.

    zones_reported=False yields the same shape with an empty value, which is
    how a hub that has not yet reported its zones presents.
    """
    value = VALVE_ZONES_TLV_PAYLOAD if zones_reported else ""
    return [{"mid": mid, "subDeviceStatus": [{"id": sid, "value": value, "time": time_ms}]}]
