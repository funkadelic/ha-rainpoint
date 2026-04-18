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

1. **Country / area code**: the phone country (calling) code for the phone number on your RainPoint account (e.g. `1` for US/Canada, `44` for UK).
2. **Email**: your RainPoint app account email.
3. **Password**: your RainPoint app account password.

After authenticating, select the **home** to monitor. There is no app-type selection; this integration uses the RainPoint app API only.

> **Heads up on API sessions.** The RainPoint cloud allows only one active session per account. Logging in here will sign you out of the RainPoint mobile app on your phone, and vice versa. To avoid that ping-pong, [create a dedicated account for Home Assistant](#use-a-dedicated-home-assistant-account-recommended) and share your home with it.

---

## Use a dedicated Home Assistant account (recommended)

Rather than giving Home Assistant your primary RainPoint credentials, create a second account and invite it to your home. Your phone keeps the original account logged in, and the integration runs on the member account, so both stay signed in at the same time.

1. In the RainPoint mobile app, sign out and create a new account with a different email address (any mailbox you control is fine).

   > **Tip:** Gmail and many other providers route `you+anything@example.com` to the same inbox as `you@example.com`. So if your main login is `user@example.com`, you can sign up the second account as `user+homeassistant@example.com` and receive both mailboxes at one address. RainPoint treats them as separate accounts.
2. Sign back in with your **original** account.
3. In the app, go to **Me → Home management → your home → Members → Invite** and invite the new account's email.
4. Accept the invitation from the new account (you can sign in briefly in a separate session, or on another device, to accept).
5. Sign back into your original account on your phone and leave it there.
6. In Home Assistant, set up this integration using the **new** account's email and password.

From then on, the new account owns the integration's session and your phone's session is never disturbed.

You can still reach every device and zone the original account can. Invited members share the same home.

---

## Entities

For each device the coordinator discovers, the integration creates:

- **Sensor entities**: one per measurement (moisture, temperature, rain, CO2, etc.) plus a disabled-by-default **Raw Payload** diagnostic sensor showing the raw hex data from the API.
- **Valve entities**: one per irrigation zone for valve hub models (HTV*).
- **Number entities**: one per zone for configuring zone run duration (1–60 minutes).
- **Hub diagnostic sensors**: RSSI, battery, firmware version, last-updated timestamp.

All entities are grouped under their parent hub device in the Home Assistant device registry.

---

## Attribution

This project is based off of [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in homeassistant-homgar.

The original MIT license is preserved. See [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup (venv, Pylance, running tests).
