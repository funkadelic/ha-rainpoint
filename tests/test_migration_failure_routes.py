"""Failure-route characterization probes for the hub identity re-key.

Written BEFORE the production code, deliberately. The re-key's failure-route
reasoning is the part of the design with no evidence behind it: every claim
about what happens when a mid cannot be resolved is inference about code that
does not exist yet. These probes pin the parts that are *not* about that code --
Home Assistant's registry behaviour, the platform one-shot property, and
listener ordering -- so the design rests on measurements instead of on argument.

Each test's docstring says which claim it settles and what the answer was.
None of these import a symbol the re-key has not written yet, so they run today.

The migration's own tests should build on these rather than re-derive them.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint.const import DOMAIN

HID = 100
MID = 200


def _hub_record(mid=MID, name="Test Hub"):
    return {
        "hid": HID,
        "mid": mid,
        "did": "did-1",
        "mac": "AA:BB:CC:DD:EE:FF",
        "productKey": "pk1",
        "model": "HWG0358WRF",
        "name": name,
        "softVer": "1.2.3",
        "subDevices": [],
    }


def _make_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "area_code": "1",
            "email": "a@b.c",
            "password": "pw",
            "hids": [HID],
            "token": "tok",
        },
        options={},
        version=1,
    )
    entry.add_to_hass(hass)
    return entry


def _make_client(device_lists):
    seq = list(device_lists)
    client = MagicMock()
    client.restore_tokens = MagicMock()
    client.export_tokens = MagicMock(return_value={})
    client.register_relogin_listener = MagicMock()
    client.list_homes = AsyncMock(return_value=[{"hid": HID, "name": "Home"}])

    async def _by_hid(hid):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    client.get_devices_by_hid = AsyncMock(side_effect=_by_hid)
    client.get_multiple_device_status = AsyncMock(return_value={})
    client.get_device_status = AsyncMock(return_value={})
    return client


async def _setup_with_patched_forward(hass, entry, client, built, monkeypatch):
    """Drive the integration's own async_setup_entry; HA's platform layer is unreachable."""
    import custom_components.rainpoint as rp

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["client"] = client

    async def fake_forward(cfg_entry, platforms):
        for platform in platforms:
            mod = importlib.import_module(f"custom_components.rainpoint.{platform}")
            captured = []

            def add(entities, update_before_add=False, _c=captured):
                _c.extend(entities)

            await mod.async_setup_entry(hass, cfg_entry, add)
            built[platform] = captured

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fake_forward)
    return await rp.async_setup_entry(hass, entry)


class TestCompetingRowIsPermanent:
    """The four-step competing-row sequence, and whether it really never recovers."""

    def test_old_and_new_shape_rows_coexist_and_collide_forever(self, hass, device_registry):
        """Claim: once a competing new-shape row exists, every later re-key attempt collides.

        ANSWER: confirmed. Two rows coexist, and the collision is not a one-off --
        it raises identically on every retry, so a retry loop cannot converge.
        """
        entry = _make_entry(hass)
        old = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}")},
            name="Original hub",
        )
        device_registry.async_update_device(old.id, name_by_user="Kitchen Hub")
        new = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}_{MID}")},
            name="Competing hub",
        )
        assert old.id != new.id

        for _ in range(3):
            with pytest.raises(dr.DeviceIdentifierCollisionError):
                device_registry.async_update_device(old.id, new_identifiers={(DOMAIN, f"hub_{HID}_{MID}")})

        survivor = device_registry.async_get(old.id)
        assert survivor.identifiers == {(DOMAIN, f"hub_{HID}")}
        assert survivor.name_by_user == "Kitchen Hub"

    def test_children_stay_on_the_abandoned_row(self, hass, device_registry):
        """Claim: the abandoned row keeps its sub-devices while carrying a dead identifier.

        ANSWER: confirmed. The child's via_device_id still points at the old row's
        device.id, so the user sees children under a hub page nothing writes to.
        """
        entry = _make_entry(hass)
        old = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}")},
            name="Original hub",
        )
        child = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{HID}_{MID}_1")},
            via_device=(DOMAIN, f"hub_{HID}"),
            name="Child",
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}_{MID}")},
            name="Competing hub",
        )

        # The failed re-key is the point: without attempting it, this test would
        # only prove that creating an unrelated third row leaves the child alone,
        # and would still pass if a failed re-key relocated children.
        with pytest.raises(dr.DeviceIdentifierCollisionError):
            device_registry.async_update_device(old.id, new_identifiers={(DOMAIN, f"hub_{HID}_{MID}")})

        assert device_registry.async_get(old.id).identifiers == {(DOMAIN, f"hub_{HID}")}
        assert device_registry.async_get(child.id).via_device_id == old.id


class TestNonNumericMidRoute:
    """The steady route: a mid the isdigit filter drops on every pass."""

    @pytest.mark.parametrize("mid", [-5, "abc", "1.5", "", "12a"])
    def test_isdigit_rejects_these_mids(self, mid):
        """Claim: a non-numeric mid is dropped by every source, on every pass.

        ANSWER: confirmed for the filter itself. str(mid).isdigit() is False for
        each of these, so no source can supply them and the row stays old-shape.
        """
        assert not str(mid).isdigit()

    def test_int_coercion_raises_on_a_non_numeric_mid(self):
        """Claim: a lowest-value tie-break on an unfiltered candidate can raise.

        ANSWER: confirmed. int("abc") raises ValueError, so an unfiltered candidate
        reaching min(..., key=int) takes the whole call down with it.
        """
        with pytest.raises(ValueError):
            int("abc")

    def test_int_coercion_silently_accepts_a_negative_mid(self):
        """Claim: the same tie-break is safe once non-numeric candidates are filtered.

        ANSWER: NOT confirmed, and this is the surprise. int(-5) succeeds, and since
        the tie-break takes the minimum, a negative candidate would be selected ahead
        of every real mid rather than raising. The isdigit filter is therefore doing
        two different jobs: preventing a raise for one class of bad value and a silent
        wrong selection for another. It must run before the tie-break for both.
        """
        assert int("-5") == -5
        assert min(["-5", "9", "10"], key=int) == "-5"

    def test_hub_identifier_carries_a_non_numeric_mid_verbatim(self, hass):
        """Claim: device.py writes the mid into the identifier verbatim, unfiltered.

        ANSWER: confirmed, and now measured against the re-keyed spelling rather
        than predicted. This assertion previously pinned the hid-only identifier
        and said in its own text that the re-key would turn it into hub_100_-5 and
        that it must then be re-read rather than patched to green. It was, and it
        does.

        This is the steady route in one line. device.py direct-indexes the mid and
        emits it whatever it is, while the migration and the residual sweep both
        drop the same value through their isdigit filter on every pass. So the
        device row stays old-shape permanently while every platform forward writes
        the new-shape identifier, which is how one hub ends up with two device
        rows and no registry read ever failed.
        """
        from custom_components.rainpoint.device import RainPointHubDevice

        dev = RainPointHubDevice(_hub_record(mid=-5))
        idents = dev.device_info["identifiers"]
        assert idents == {(DOMAIN, f"hub_{HID}_-5")}, (
            "device.py emits the mid verbatim, so a value the isdigit filter drops still "
            "reaches the identifier the platforms write"
        )


class TestPlatformOneShotProperty:
    """Source 2 of the gate-lossless argument: the connectivity entity is one-shot."""

    @pytest.mark.asyncio
    async def test_no_hub_entities_are_created_on_a_later_poll(self, hass, device_registry, monkeypatch):
        """Claim: a hub absent from the first poll gets no entities, then or later.

        ANSWER: confirmed for every hub platform including binary_sensor, which is
        the connectivity entity's platform. That entity is one of the two registry-backed
        sources a residual re-key could otherwise wait on, so its being one-shot is what
        makes waiting on it pointless.
        """
        entry = _make_entry(hass)
        client = _make_client([[], [_hub_record()]])
        built = {}
        assert await _setup_with_patched_forward(hass, entry, client, built, monkeypatch) is True
        assert sum(len(v) for v in built.values()) == 0
        assert built["binary_sensor"] == []

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_refresh()
        assert [h["mid"] for h in coordinator.data["hubs"]] == [MID]
        assert sum(len(v) for v in built.values()) == 0


class TestListenerOrdering:
    """The composite ordering claim, measured through the existing analog."""

    @pytest.mark.asyncio
    async def test_setup_registered_listener_fires_before_the_late_adders(self, hass, device_registry, monkeypatch):
        """Claim: a listener registered in async_setup_entry runs before sensor.py's adder.

        ANSWER: confirmed, using _reconcile_sub_device_parents_on_updates as the
        stand-in for the not-yet-written re-key wrapper. It registers at the same
        point in async_setup_entry, so the ordering result transfers to any listener
        registered there. Fire order is recorded from a real refresh, not asserted
        from registration order.

        NOTE, because it is easy to get wrong: under this harness RainPointCoordinator
        extends tests/conftest.py's DataUpdateCoordinator stub, NOT Home Assistant's.
        The stub keeps _listeners as a LIST of bare callbacks and fires them in
        append order; the real class keeps an insertion-ordered dict of
        (callback, context) tuples. Registration order equals fire order in both,
        so the property under test holds either way, but any test that reaches into
        _listeners must handle the list form -- the dict form never runs here.
        """
        entry = _make_entry(hass)
        client = _make_client([[_hub_record()]])
        built = {}
        assert await _setup_with_patched_forward(hass, entry, client, built, monkeypatch) is True

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        assert isinstance(coordinator._listeners, list), (
            "conftest's stub is expected here; if this is a dict the real "
            "DataUpdateCoordinator is in play and the unwrapping below needs updating"
        )

        fired = []

        def make(cb, lbl):
            def _recording():
                fired.append(lbl)
                return cb()

            return _recording

        coordinator._listeners = [make(cb, f"{cb.__module__}.{cb.__qualname__}") for cb in coordinator._listeners]

        coordinator.async_update_listeners()

        assert fired, "no coordinator listeners fired"
        setup_time = [i for i, lbl in enumerate(fired) if "reconcile_sub_device_parents" in lbl]
        late_adders = [i for i, lbl in enumerate(fired) if any(m in lbl for m in (".sensor.", ".valve.", ".number."))]
        assert setup_time, f"setup-registered listener not found in {fired}"
        # Unconditional on purpose: guarding this behind `if late_adders` would let
        # it pass without testing anything the moment platform setup or listener
        # registration changed, which is the failure this test exists to catch.
        assert late_adders, f"no late adder listeners registered; ordering is untested in {fired}"
        assert max(setup_time) < min(late_adders), fired
