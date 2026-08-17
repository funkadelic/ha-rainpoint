"""The two buttons that run the HIC control-encoding probe.

Kept out of ``button.py`` for the same reason ``generic_control.py`` is kept
out of ``valve.py``: this is an opt-in diagnostic surface with its own gate,
and the shipped button platform should not have to reason about it.

Both buttons exist only when the owner has turned the probe on in the options,
and both are CONFIG-category so they sit with the device's settings rather than
among its readings. See ``control_probe.py`` for why the probe exists and what
keeps it from being dangerous.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, HIC_PROBE_STATION, UNIQUE_ID_PREFIX
from .control_probe import async_run_probe
from .coordinator import RainPointCoordinator
from .entity import RainPointSubDeviceEntity

_LOGGER = logging.getLogger(__name__)

# Where a finished run is parked for the diagnostics dump to find. One slot per
# stage, so running the station walk never overwrites the rain-delay answer the
# owner may not have reported yet.
PROBE_RESULT_STORE_KEY = "hic_control_probe"

# The same record again, on disk. Two consecutive support round trips were lost
# because the in-memory copy above is the only one that existed: a restart
# between the press and the download clears it, and the owner has no way to
# know that happened. This file survives both a restart and a config entry
# reload, so the answer stops depending on the owner completing two steps
# without Home Assistant being touched in between.
PROBE_PERSIST_VERSION = 1
PROBE_PERSIST_KEY = f"{DOMAIN}.hic_control_probe"

KIND_RAIN_DELAY = "rain_delay"
KIND_STATION = "station"


def store_probe_result(hass: Any, entry_id: str, kind: str, result: dict) -> None:
    """Record a finished run against its stage.

    Tolerant of a missing entry store rather than raising: this is called at
    the end of a button press whose real work has already happened, and losing
    the record is a worse outcome than nothing only if it takes the press down
    with it.
    """
    store = (hass.data.get(DOMAIN) or {}).get(entry_id)
    if store is None:
        _LOGGER.warning("HIC probe: no entry store to record the %s run against", kind)
        return
    store.setdefault(PROBE_RESULT_STORE_KEY, {})[kind] = result


def probe_results(hass: Any, entry_id: str) -> dict:
    """Return this session's recorded runs for this entry, or {} when none."""
    store = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    return store.get(PROBE_RESULT_STORE_KEY) or {}


async def _async_load_persisted(hass: Any) -> dict:
    """Return the whole saved file, or {} when there is nothing readable in it.

    Every failure answers {} rather than raising, for the reason the in-memory
    writer gives: the caller is either finishing a press whose real work has
    already happened, or building a diagnostics dump. Neither is worth taking
    down over a store that would not read.
    """
    try:
        saved = await Store(hass, PROBE_PERSIST_VERSION, PROBE_PERSIST_KEY).async_load()
    except Exception as exc:
        _LOGGER.warning("HIC probe: the saved runs could not be read: %s", type(exc).__name__)
        return {}
    return saved if isinstance(saved, dict) else {}


async def async_record_probe_result(hass: Any, entry_id: str, kind: str, result: dict) -> None:
    """Record a finished run in memory and on disk, in that order.

    Memory first and unconditionally: it is the copy that cannot fail, and a
    press whose disk write fails should still show its result in a dump taken
    before the next restart.
    """
    store_probe_result(hass, entry_id, kind, result)
    saved = await _async_load_persisted(hass)
    saved.setdefault(entry_id, {})[kind] = result
    try:
        await Store(hass, PROBE_PERSIST_VERSION, PROBE_PERSIST_KEY).async_save(saved)
    except Exception as exc:
        _LOGGER.warning("HIC probe: the %s run could not be saved to disk: %s", kind, type(exc).__name__)


async def async_probe_results(hass: Any, entry_id: str) -> dict:
    """Return every recorded run for this entry, disk and memory merged.

    This session's runs win per stage. A run held only in memory is one whose
    disk write failed, and a run held only on disk is one from before the last
    restart; the owner is owed both, and the fresher of the two when a stage has
    each.
    """
    merged = dict((await _async_load_persisted(hass)).get(entry_id) or {})
    merged.update(probe_results(hass, entry_id))
    return merged


class _RainPointProbeButton(RainPointSubDeviceEntity, ButtonEntity):
    """Shared press handling for both probe stages."""

    _attr_entity_category = EntityCategory.CONFIG

    _kind: str = ""

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._entry_id = entry_id

    @property
    def available(self) -> bool:
        """Available whenever the device is, without requiring a decoded reading.

        The base class reads availability off the decoded payload, which is the
        right rule for a reading. A probe button is not a reading: its whole
        purpose is to act on a device whose command path is unknown, and a
        frame this integration failed to decode is exactly when an owner is
        most likely to have been asked to press it.
        """
        return True

    async def async_press(self) -> None:
        """Run this stage's candidate walk and record the result.

        Exceptions are deliberately not caught here. The probe already scores a
        refused or failed call as an ordinary recorded outcome and carries on,
        so anything reaching this far is a fault in the probe itself rather
        than the device declining a command, and Home Assistant surfacing it to
        the owner synchronously is the honest outcome.
        """
        client = (self.hass.data[DOMAIN][self._entry_id]).get("client")
        live = ((self.coordinator.data or {}).get("sensors") or {}).get(self._sensor_key) or self._sensor_info
        # Both lines are WARNING for the reason _log_attempt gives: a default
        # install records WARNING and above, and the first real run came back
        # with a log that carried nothing but the line saying the buttons had
        # loaded, because this pair was INFO.
        _LOGGER.warning("HIC probe: starting the %s walk", self._kind)
        run = await async_run_probe(client, live, kind=self._kind, now=dt_util.utcnow().isoformat())
        run.finished_at = dt_util.utcnow().isoformat()
        await async_record_probe_result(self.hass, self._entry_id, self._kind, run.as_dict())
        _LOGGER.warning(
            "HIC probe: %s walk finished after %d attempts, confirmed=%s",
            self._kind,
            len(run.attempts),
            run.confirmed_label,
        )


class RainPointProbeRainDelayButton(_RainPointProbeButton):
    """Stage 1. Sets a rain delay, which waters nothing under any encoding."""

    _kind = KIND_RAIN_DELAY
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug, entry_id):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug, entry_id)
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{base_slug}_probe_rain_delay"
        self._attr_name = "Probe Rain Delay Command"


class RainPointProbeStationButton(_RainPointProbeButton):
    """Stage 2. Tries to start one station, then stops it again."""

    _kind = KIND_STATION
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug, entry_id):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug, entry_id)
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{base_slug}_probe_station"
        self._attr_name = f"Probe Station {HIC_PROBE_STATION} Command"
