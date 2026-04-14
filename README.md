# RainPoint Cloud

[![Build](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml/badge.svg)](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml)
[![Codecov](https://img.shields.io/codecov/c/github/funkadelic/ha-rainpoint?logo=codecov)](https://codecov.io/gh/funkadelic/ha-rainpoint)
[![Release](https://img.shields.io/github/release/funkadelic/ha-rainpoint.svg)](https://github.com/funkadelic/ha-rainpoint/releases)
[![License](https://img.shields.io/github/license/funkadelic/ha-rainpoint.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-blue.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for RainPoint Smart+ irrigation devices via the RainPoint cloud API.

---

## Supported Devices

This integration supports RainPoint Smart+ device families, including:

| Family | Examples | Entities Created |
| ------ | -------- | ---------------- |
| Valve hubs | HTV245FRF (primary tested device), HTV213FRF, HTV0540FRF | Valve per zone, duration number per zone |
| Soil sensors | HCS021FRF, HCS026FRF, HCS003FRF, HCS005FRF | Moisture, temperature, illuminance |
| Rain sensors | HCS012ARF | Hourly / daily / weekly / total rainfall |
| Temperature & humidity | HCS014ARF, HCS027ARF, HCS016ARF | Temperature, humidity |
| Weather stations | HWS019WRF-V2 | Display hub diagnostics |
| Pool sensors | HCS0528ARF, HCS015ARF | Pool temperature, ambient |
| CO2 / env sensors | HCS0530THO | CO2, temperature, humidity |
| Flow meters | HCS008FRF | Flow reading |

The **HTV245FRF** wifi valve is the primary tested device and the integration's core-value target. Other models are supported opportunistically from captured payloads.

All devices communicate via the RainPoint cloud backend. There is no local LAN protocol.

---

## Installation via HACS

1. In Home Assistant, open **HACS** from the sidebar.
2. Click the three-dot menu and select **Custom repositories**.
3. Add `funkadelic/ha-rainpoint` with category **Integration**.
4. Search for **RainPoint Cloud** in the HACS integration list and install it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for **RainPoint Cloud**.

---

## Configuration

The config flow asks for three fields:

1. **Country / area code** — the phone country (calling) code for the phone number on your RainPoint account (e.g. `1` for US/Canada, `44` for UK).
2. **Email** — your RainPoint app account email.
3. **Password** — your RainPoint app account password.

After authenticating, select the **home** to monitor. There is no app-type selection — this integration uses the RainPoint app API only.

**Note on API sessions:** The RainPoint cloud API allows only one active session per account. Logging in via this integration will log you out of the RainPoint mobile app. Consider creating a dedicated API account and inviting it to your home as a member.

---

## Entities

For each device the coordinator discovers, the integration creates:

- **Sensor entities** — one per measurement (moisture, temperature, rain, CO2, etc.) plus a disabled-by-default **Raw Payload** diagnostic sensor showing the raw hex data from the API.
- **Valve entities** — one per irrigation zone for valve hub models (HTV*).
- **Number entities** — one per zone for configuring zone run duration (1–60 minutes).
- **Hub diagnostic sensors** — RSSI, battery, firmware version, last-updated timestamp.

All entities are grouped under their parent hub device in the Home Assistant device registry.

---

## Migrating from homeassistant-homgar

**This fork is NOT a drop-in replacement for the upstream `homeassistant-homgar` integration.**

To migrate:

1. In Home Assistant, go to **Settings → Devices & Services**.
2. Find the existing **HomGar** (or HomGar/RainPoint) integration entry and remove it completely — delete the config entry.
3. Remove the `custom_components/homgar/` folder from your Home Assistant `config/` directory (or let HACS uninstall it).
4. Install **RainPoint Cloud** fresh via HACS (see Installation above).
5. Re-add the integration through **Settings → Devices & Services → Add Integration → RainPoint Cloud**.

**Entity IDs will change.** All entity IDs move from `sensor.homgar_*` / `valve.homgar_*` etc. to `sensor.rainpoint_*` / `valve.rainpoint_*`. Any automations, dashboards, or scripts referencing the old entity IDs must be updated to use the new ones.

---

## Attribution

This project is a fork of [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz. The original project supports both HomGar and RainPoint apps; this fork narrows to the RainPoint app only.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in the upstream project.

The original MIT license is preserved — see [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. The primary testing target is the HTV245FRF valve — if you have other RainPoint hardware and can provide raw payloads, open an issue to help expand decoder coverage.
