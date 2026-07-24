# RainPoint Cloud

[![Build](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml/badge.svg)](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml)
[![Codecov](https://img.shields.io/codecov/c/github/funkadelic/ha-rainpoint?logo=codecov)](https://codecov.io/gh/funkadelic/ha-rainpoint)
[![Release](https://img.shields.io/github/release/funkadelic/ha-rainpoint.svg)](https://github.com/funkadelic/ha-rainpoint/releases)
[![License](https://img.shields.io/github/license/funkadelic/ha-rainpoint.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-blue.svg)](https://github.com/hacs/default)

A Home Assistant custom integration for RainPoint Smart+ irrigation devices via the RainPoint cloud API.

---

## Supported Devices

This integration supports RainPoint Smart+ device families, including:

| Family | Examples | Entities Created |
| ------ | -------- | ---------------- |
| Valve hubs | HTV245FRF (primary tested device), HTV113FRF, HTV145FRF, HTV213FRF, HTV345FRF, HTV405FRF, HTV0540FRF | Valve per zone, duration number per zone |
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

This integration is part of the default HACS store, so no custom repository is needed.

1. In Home Assistant, open **HACS** from the sidebar.
2. Search for **RainPoint Cloud** and open it.
3. Click **Download** to install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **RainPoint Cloud**.

> Installing an older release? Versions published before HACS default inclusion may require adding `funkadelic/ha-rainpoint` as a custom repository (category **Integration**) first.

---

## Configuration

The config flow asks for three fields:

1. **Country**: select the country for your RainPoint account from the dropdown. This is Home Assistant's standard localized country picker and now lists every country. The integration looks up the matching phone dial code from your selection automatically, so there is no separate dial-code field to fill in.
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

## Real-time push updates (opt-in)

In addition to the 120-second polling that always runs, the integration can optionally surface device state changes in near real time over an MQTT push connection. Push is additive, opt-in, and off by default, and polling keeps running as the fallback no matter what, so nothing breaks if you leave push off or the connection drops.

### Enabling or disabling push

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. In the **RainPoint Push Channel** form, check **Enable push updates** (unchecked by default).
3. Save. The change applies automatically (the integration reloads itself), so you never have to reload or re-add it by hand.

To turn push back off, revisit the same **Configure** screen and uncheck **Enable push updates**.

### Telling whether push is working

Enabling push adds two hub-level diagnostic entities: **`<hub> Push Connected`** (on when the MQTT client is connected) and **`<hub> Push Last Message`** (timestamp of the last message received). If the push connection drops and stays down while polling keeps devices updating, Home Assistant raises a **Settings → Repairs** issue so you know to look. (A channel that stays connected but quietly stops sending updates looks the same as an idle one, so that case is not flagged.)

### Account implications

> **Push is a convenience, not a safety-critical replacement for the RainPoint app.** It runs over an unofficial MQTT connection to RainPoint's cloud (Alibaba Cloud IoT) that was pieced together by reverse engineering, so it stays opt-in and off by default. In testing it ran alongside the RainPoint mobile app without pushing either one offline. The vendor hands out MQTT connection slots from a small shared pool, so once in a while push and the app can briefly collide and one of them drops; Home Assistant reconnects on its own, and the 120-second polling keeps devices current in the meantime.

The push connection is a separate MQTT connection from the HTTP login session used for setup and polling, so the [one-active-session-per-account guidance above](#use-a-dedicated-home-assistant-account-recommended) and the existing session warning under **Configuration** still apply unchanged. The coexistence finding above is specific to the MQTT push channel, not the HTTP login.

---

## Attribution

This project is based on [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in homeassistant-homgar.

The original MIT license is preserved. See [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup (venv, Pylance, running tests).
