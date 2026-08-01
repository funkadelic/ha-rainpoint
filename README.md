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
| Valve hubs | HTV245FRF, HTV113FRF, HTV145FRF, HTV213FRF, HTV345FRF, HTV405FRF, HTV0540FRF | Valve per zone, duration number per zone |
| Soil sensors | HCS021FRF, HCS026FRF (tested device), HCS005FRF, HCS024FRF-V1 | Moisture, temperature, illuminance |
| Rain sensors | HCS012ARF | Hourly / daily / weekly / total rainfall |
| Temperature & humidity | HCS014ARF | Temperature, humidity |
| Weather stations | HWS019WRF-V2 | Display hub diagnostics |
| Pool sensors | HCS0528ARF, HCS015ARF, HCS015ARF+ | Pool temperature, ambient |
| CO2 / env sensors | HCS0530THO | CO2, temperature, humidity |
| Flow meters | HCS008FRF | Flow reading |
| Bluetooth valves | HTV210B (tested device, hub-paired) | Battery, signal strength, per-zone open/closed state |

The **HTV245FRF** wifi valve, the **HCS026FRF** soil sensor, and the **HTV210B** Bluetooth valve are the maintainer's own hardware and are the models tested against real devices. Other models are supported opportunistically from captured payloads.

The **HTV210B** only reports to the cloud while paired through a hub; used over Bluetooth alone, RainPoint still lists it under the hub but the integration surfaces it as a not-reporting device rather than dropping it silently, since no readings and no control are available in that state. Its zone sensors are read-only when it is reporting: opening and closing from the app works over the cloud, but the exact command the integration would need to send is still being verified, so no valve entity is created rather than one that might accept a command the hardware never acts on.

Every model listed above has a decoder written against a real payload. A model that is absent is not necessarily unusable: the opt-in generic sensors described under [Configuration](#configuration) can often surface readings for it from the product catalog, clearly labeled unverified.

All devices communicate via the RainPoint cloud backend. There is no local LAN protocol.

---

## My device isn't listed

There are two different ways a device can look unsupported, and each produces a different surface.

**A device that reports a payload the integration cannot decode.** This doesn't break anything: the integration keeps polling, marks the device as `unknown`, and adds a disabled-by-default **Raw Payload** diagnostic sensor holding its raw data. It also raises a Home Assistant notification with a one-click link that opens a **New device support** report pre-filled with the model and payload.

**A device RainPoint lists on the hub but that returns no readings at all.** This is different: there is no payload, so there is no Raw Payload sensor to hold one. Instead, Home Assistant raises a **Settings → Repairs** issue naming the device, and the device gets a single **Not Reporting** diagnostic entity whose state says whether it has never reported or last reported at a given time. That entity's attributes carry the same pre-filled report link, with the payload field stating plainly that the device returns no status. This commonly means the device is paired over Bluetooth only, out of range, or switched off. The integration clears the Repairs issue on its own once readings resume; the Not Reporting entity stays on the device and simply stops reporting a state. The report is still worth filing here: the absence of a payload is itself the finding.

Neither surface is instant. The integration waits until a device has been missing from three consecutive successful checks before treating it as not reporting, which is roughly four to six minutes at the default two-minute polling interval. A check that failed because the hub itself could not be reached does not count against the device, so a hub going offline does not make its healthy children look silent. Once that threshold is crossed, both surfaces appear together in the session that is already running: the Repairs issue is raised and the Not Reporting entity is added at the same point, so a device that goes quiet while Home Assistant is running becomes visible with no further action from you.

To get your device added:

1. Open a [New device support issue](https://github.com/funkadelic/ha-rainpoint/issues/new?template=new_device.yml) (the notification link, or the Not Reporting entity's report link, pre-fills the model and whatever payload is available for you).
2. Include raw payloads in a few known states (valve closed vs open, a sensor at a known reading). One capture shows the byte layout; different states reveal what each byte means. See [`DEBUG_VALVE_PAYLOAD.md`](DEBUG_VALVE_PAYLOAD.md) for how to capture. A device that never reports has no payload to capture; describe what you see in the RainPoint app instead.

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

1. **Country**: select the country for your RainPoint account from the dropdown.
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

- **Sensor entities**: one per measurement (moisture, temperature, rain, CO2, etc.) plus a disabled-by-default **Raw Payload** diagnostic sensor showing the raw hex data from the API. A device that returns no readings at all gets a single **Not Reporting** diagnostic entity instead, and no Raw Payload sensor, because there is no payload to show.
- **Valve entities**: one per irrigation zone, for the valve hub models listed in the table above. The HTV210B is not one of them: its zone state is read-only, as described under [Supported devices](#supported-devices).
- **Number entities**: one per zone for configuring zone run duration (1 to 60 minutes), on those same valve hub models.
- **Hub diagnostic sensors**: RSSI, battery, firmware version, last-updated timestamp.
- **Hub Cloud Connection**: one binary sensor per hub, on when RainPoint's cloud currently reports that hub as reachable. It exists whether or not [push](#real-time-push-updates-opt-in) is enabled.

All entities are grouped under their parent hub device in the Home Assistant device registry.

---

## When a hub goes offline

Each hub's **Cloud Connection** entity reflects whether RainPoint's cloud currently reports that hub as reachable, refreshed on every poll (every two minutes by default). With [push](#real-time-push-updates-opt-in) enabled, RainPoint also announces a hub going offline or coming back as it happens, and the entity follows within a second or so instead of waiting for the next poll. The poll keeps running either way, so nothing depends on push being on.

If a hub stays unreachable for three checks in a row, roughly four to six minutes at the default two-minute polling interval, Home Assistant also raises a **Settings → Repairs** notice naming the hub, so a brief blip doesn't flap a card on and off. Valve controls for devices on that hub become unavailable as soon as the hub is reported offline, which is within a single check, or near-immediately with push enabled, since a command cannot reach hardware the cloud itself cannot reach.

Every other reading on that hub keeps showing its last known value, and that deserves its own explanation. RainPoint keeps serving the last reading it received for every device on an offline hub, for as long as the outage lasts, rather than reporting that a device has gone quiet. That means a reading can look perfectly current in Home Assistant while the hub behind it has actually been offline for hours: the moisture, temperature, or rainfall value shown is the last one RainPoint delivered, not necessarily a fresh one. The integration leaves those readings visible rather than hiding them, because the data catches up within seconds of the hub reconnecting, and marking devices unavailable for a condition that clears itself would leave gaps in your history and break any automation or template built around a device going offline.

To tell whether what you're looking at is current, check two things: the hub's own **Cloud Connection** entity, and the `hub_connected` attribute Home Assistant now attaches to every entity on that hub's devices. The attribute is always present: `hub_connected` is `true` when the cloud reports the hub connected, `false` when it reports the hub offline, and `none` when the cloud hasn't said either way yet. Test that unknown state with `is none` rather than `is defined`, since the key exists either way. A dashboard card or an automation condition can check this attribute directly to flag a reading that might be stale.

Everything above clears on its own after the hub reconnects: the Repairs notice closes, the Cloud Connection entity turns back on, valve controls return, and no reload is needed. With push enabled that happens within seconds of the hub coming back; otherwise it waits for the next scheduled check, so allow up to two minutes.

---

## Real-time push updates (opt-in)

In addition to the 120-second polling that always runs, the integration can optionally surface device state changes in near real time over an MQTT push connection. Push is additive, opt-in, and off by default, and polling keeps running as the fallback no matter what, so nothing breaks if you leave push off or the connection drops.

Push carries two kinds of update: new readings from individual devices, and a hub going offline or coming back. The second is what makes [a hub outage](#when-a-hub-goes-offline) visible almost immediately rather than up to two minutes later. Note this speeds up the Cloud Connection entity and the recovery side, not the Repairs notice itself: that notice still waits for three consecutive checks before it appears, deliberately, so a brief blip doesn't flap a card on and off.

### Enabling or disabling push

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. In the **RainPoint Push Channel** form, check **Enable push updates** (unchecked by default).
3. Save. The change applies automatically (the integration reloads itself), so you never have to reload or re-add it by hand.

To turn push back off, revisit the same **Configure** screen and uncheck **Enable push updates**.

### Telling whether push is working

Enabling push adds two hub-level diagnostic entities: **`<hub> Push Connected`** (on when the MQTT client is connected) and **`<hub> Push Last Message`** (timestamp of the last message received). If the push connection drops and stays down while polling keeps devices updating, Home Assistant raises a **Settings → Repairs** issue so you know to look. (A channel that stays connected but quietly stops sending updates looks the same as an idle one, so that case is not flagged.)

### Notes on push

Push is off by default for now while it proves out; you can turn it on at any time. If the connection ever drops, Home Assistant reconnects on its own, and the usual 120-second polling keeps your devices up to date in the meantime. In testing it ran alongside the RainPoint phone app without knocking either one offline.

Turning push on doesn't change the [one-session-per-account note above](#use-a-dedicated-home-assistant-account-recommended): it's the sign-in used for setup that can bump your phone out of the app (and vice versa), whether or not push is enabled.

---

## Attribution

This project is based on [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in homeassistant-homgar.

The original MIT license is preserved. See [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup (venv, Pylance, running tests).
