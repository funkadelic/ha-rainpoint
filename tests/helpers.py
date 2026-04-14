"""Shared test utility functions for RainPoint integration tests."""


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
