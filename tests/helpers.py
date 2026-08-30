"""Shared test utility functions for RainPoint integration tests.

``make_sensor_coordinator`` is the shared coordinator builder used by the entity
test modules (sensor, valve, number, diagnostic_sensors) so they do not each
rehydrate the canonical ``{"hubs", "status", "sensors"}`` shape inline.

``make_mock_session_client`` and ``mock_json_response`` are the client-level
counterparts: a real ``RainPointClient`` wired to a mocked aiohttp session, for
tests that need to assert on the exact JSON body a real client method builds
rather than on a mocked call.
"""

from unittest.mock import AsyncMock, MagicMock

from custom_components.rainpoint.api import RainPointClient
from custom_components.rainpoint.const import MODEL_HTV210B
from tests.payload_samples import SAMPLE_HTV210B_TLV_PAYLOAD

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

VALVE_ZONE1_OPEN_TLV_PAYLOAD = "11#17E1D70018DC0119D8011AD8001D201E2025AD580221B740511B1A"
"""VALVE_ZONES_TLV_PAYLOAD with zone 1 reporting open, plus a duration and an
event-time record on zone 1's own dp_ids.

Zone 1's 0xD8 state byte is flipped from 0x00 to 0x01, and two records are
appended: dp 0x25 type 0xAD carrying 600 (10 minutes) as a 2-byte
little-endian seconds count, and dp 0x21 type 0xB7 carrying a packed
timestamp that decodes to 2026-08-13T21:05:00. Zone 2 is untouched and still
decodes closed with no duration and no event time. This is used by more than
one duration-entity test class to drive a real closed-then-open-then-closed
coordinator timeline, so it lives here rather than in whichever module
happened to need it first; the byte layout is not asserted in prose anywhere
that uses it, only through decoding the payload and reading the resulting
zone dict.
"""


def make_mock_session_client() -> RainPointClient:
    """Create a real RainPointClient with a mocked aiohttp session.

    Constructor args: area_code, email, password, session. ``_token`` is set
    directly so ``_auth_headers()`` does not raise. Moved here from
    ``tests/api/test_client.py``'s module-level ``_make_client`` so a test in
    any module can assert on the exact JSON body a real client method builds,
    not just on a mocked call.
    """
    mock_session = MagicMock()
    client = RainPointClient(
        area_code="1",
        email="test@example.com",
        password="testpass",
        session=mock_session,
    )
    client._token = "fake-token-for-test"
    return client


def mock_json_response(json_data: dict, status: int = 200) -> AsyncMock:
    """Create a mock aiohttp response context manager returning json_data.

    Moved here from ``tests/api/test_client.py``'s module-level
    ``_mock_response`` for the same reason as ``make_mock_session_client``.
    """
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    # aiohttp uses async context manager for session.post()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


def make_valve_zone_status(mid=20, sid="D01", zones_reported=True, time_ms=1785420002247):
    """Return a multipleDeviceStatus list for one valve hub child.

    zones_reported=False yields the same shape with an empty value, which is
    how a hub that has not yet reported its zones presents.
    """
    value = VALVE_ZONES_TLV_PAYLOAD if zones_reported else ""
    return [{"mid": mid, "subDeviceStatus": [{"id": sid, "value": value, "time": time_ms}]}]


def make_valve_zone_status_open(mid=20, sid="D01", time_ms=1785420002247):
    """The open-zone counterpart of make_valve_zone_status.

    Reports zone 1 explicitly open (with a duration and an event time) and
    zone 2 explicitly closed, via VALVE_ZONE1_OPEN_TLV_PAYLOAD. Exists so
    more than one module can drive a real open-zone timeline through a real
    coordinator, the same way make_valve_zone_status already does for the
    closed case.
    """
    return [{"mid": mid, "subDeviceStatus": [{"id": sid, "value": VALVE_ZONE1_OPEN_TLV_PAYLOAD, "time": time_ms}]}]


def htv210b_hub_devices(mid=20, addr=1, model_code=41):
    """A getDeviceByHid hub record carrying one hub-paired HTV210B sub-device.

    model_code is a parameter so a test can report the model under a code the
    committed catalog does not carry, which is what a catalog refresh looks
    like from the integration's side.
    """
    return [
        {
            "mid": mid,
            "name": "Hub A",
            "deviceName": "hub-mac",
            "productKey": "hub-pk",
            "homeName": "H",
            "subDevices": [{"addr": addr, "name": "BT Valve", "model": MODEL_HTV210B, "modelCode": model_code, "softVer": "1.0"}],
        }
    ]


def htv210b_status(mid=20, sid="D01"):
    """A multipleDeviceStatus reading for the HTV210B, both zones idle."""
    return [{"mid": mid, "subDeviceStatus": [{"id": sid, "value": SAMPLE_HTV210B_TLV_PAYLOAD, "time": 1785420002247}]}]


def htv210b_silent_status(mid=20):
    """A multipleDeviceStatus poll that carries no entry for the HTV210B.

    The silent form must return no status entry for the sub-device at all,
    rather than an empty or malformed one: a malformed entry exercises the
    record-tolerance path instead of the silence path this helper feeds. The
    hub itself is still enumerated, matching a real cloud outage where a
    sub-device stops answering while its hub keeps polling fine.
    """
    return [{"mid": mid, "subDeviceStatus": []}]
