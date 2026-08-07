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
| Bluetooth valves | HTV210B (tested device, hub-paired) | Battery, signal strength, per-zone open/closed state, per-zone open/close control and run duration |

The **HTV245FRF** wifi valve, the **HCS026FRF** soil sensor, and the **HTV210B** Bluetooth valve are the maintainer's own hardware and are the models tested against real devices. Other models are supported opportunistically from captured payloads.

The **HTV210B** only reports to the cloud while paired through a hub. Used over Bluetooth alone, RainPoint still lists it under the hub, but the integration surfaces it as a not-reporting device rather than dropping it silently, since no readings and no control are available in that state. No valve entity is created for it in that state either, so a control that provably cannot reach the hardware is never offered.

While it is hub-paired, its zones open and close from Home Assistant like any other valve, with a run duration per zone. This valve needs a different command path from the wifi valves, which is why it was read-only in earlier releases.

If you ran an earlier release, the read-only zone state sensors it created stay where they are, so each zone now has both a state sensor and a valve control. Nothing is deleted for you, because automations and dashboard cards may already point at those sensors. If you would rather see only the valve, disable the zone state sensors from the device page.

Every model listed above has a decoder written against a real payload. A model that is absent is not necessarily unusable: the [opt-in generic entities](#unverified-generic-entities-opt-in) can often surface readings for it from the product catalog, clearly labeled unverified.

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
- **Valve entities**: one per irrigation zone, for the valve models listed in the table above, including the HTV210B while it is hub-paired. A device the integration cannot currently reach gets no valve entity, as described under [Supported devices](#supported-devices).
- **Number entities**: one per zone for configuring zone run duration (1 to 60 minutes), on those same valve models. The duration applies to the next run: changing it while a zone is already open does not shorten or extend the run in progress.
- **Hub diagnostic sensors**: RSSI, battery, firmware version, last-updated timestamp.
- **Hub Cloud Connection**: one binary sensor per hub, on when RainPoint's cloud currently reports that hub as reachable. It exists whether or not [push](#real-time-push-updates) is enabled.

All entities are grouped under their parent hub device in the Home Assistant device registry.

---

## When a hub goes offline

Each hub's **Cloud Connection** entity reflects whether RainPoint's cloud currently reports that hub as reachable, refreshed on every poll (every two minutes by default). With [push](#real-time-push-updates) enabled, RainPoint also announces a hub going offline or coming back as it happens, and the entity follows within a second or so instead of waiting for the next poll. The poll keeps running either way, so nothing depends on push being on.

If a hub stays unreachable for three checks in a row, roughly four to six minutes at the default two-minute polling interval, Home Assistant also raises a **Settings → Repairs** notice naming the hub, so a brief blip doesn't flap a card on and off. Valve controls for devices on that hub become unavailable as soon as the hub is reported offline, which is within a single check, or near-immediately with push enabled, since a command cannot reach hardware the cloud itself cannot reach.

Every other reading on that hub keeps showing its last known value, and that deserves its own explanation. RainPoint keeps serving the last reading it received for every device on an offline hub, for as long as the outage lasts, rather than reporting that a device has gone quiet. That means a reading can look perfectly current in Home Assistant while the hub behind it has actually been offline for hours: the moisture, temperature, or rainfall value shown is the last one RainPoint delivered, not necessarily a fresh one. The integration leaves those readings visible rather than hiding them, because the data catches up within seconds of the hub reconnecting, and marking devices unavailable for a condition that clears itself would leave gaps in your history and break any automation or template built around a device going offline.

To tell whether what you're looking at is current, check two things: the hub's own **Cloud Connection** entity, and the `hub_connected` attribute Home Assistant now attaches to every entity on that hub's devices. The attribute is always present: `hub_connected` is `true` when the cloud reports the hub connected, `false` when it reports the hub offline, and `none` when the cloud hasn't said either way yet. Test that unknown state with `is none` rather than `is defined`, since the key exists either way. A dashboard card or an automation condition can check this attribute directly to flag a reading that might be stale.

Everything above clears on its own after the hub reconnects: the Repairs notice closes, the Cloud Connection entity turns back on, valve controls return, and no reload is needed. With push enabled that happens within seconds of the hub coming back; otherwise it waits for the next scheduled check, so allow up to two minutes.

---

## When a device's entities are left over

RainPoint sometimes moves a device to a different parent record on its side, which changes the identity this integration builds its entity IDs from. A fresh set of entities then appears for the same physical device while the old set stays behind, permanently unavailable, so every reading looks like it exists twice.

Home Assistant raises a **Settings → Repairs** card, "A device's entities are left over from an older listing", once the old listing has been absent from thirty consecutive checks, roughly an hour at the default two-minute polling interval. The window is deliberately long, and it pauses entirely while the device's hub is itself missing from your account listing, so a cloud-side blip cannot strand a healthy device's entities.

Nothing is removed automatically. Short of deleting entities yourself under **Settings → Devices & services → Entities**, that card's **Submit** button is the only thing that removes an entity you did not opt into, and it deletes the history recorded against those entities along with them, which cannot be undone. (The one other integration-side deletion is switching off an option you turned on yourself, under [unverified generic entities](#unverified-generic-entities-opt-in), which clears the entities that option created.) If you would rather keep the history, leave the card alone: the leftover entities stay where they are, unavailable but intact. The leftover device page is released at the same time as the entities, once it carries nothing else.

One thing to know before deferring it: the card is withdrawn when the integration reloads or Home Assistant restarts, and it is not raised again, because the old listing is gone from your account and nothing is left to notice its entities. Removing them after that means removing them by hand under **Settings → Devices & services → Entities**.

---

## Real-time push updates

In addition to the 120-second polling that always runs, the integration surfaces device state changes in near real time over an MQTT push connection. Push is additive and on by default: polling keeps running as the fallback no matter what, so nothing breaks whether push is on, off, or the connection drops. Turning it off is a supported choice, not a workaround.

Poll and push do different jobs, not the same job at different speeds, which is why polling can never be turned off. Every 120 seconds, poll rebuilds the full picture of your account from scratch: it is what notices a device or hub added to the account, and what notices a hub that has quietly left it. Push carries no such information, only state changes for devices poll already knows about, so it can never take over poll's discovery role. Push exists purely to shorten how long a known device's reading, or a known hub's online or offline state, takes to reach Home Assistant, from up to two minutes down to about a second.

Push carries two kinds of update: new readings from individual devices, and a hub going offline or coming back. The second is what makes [a hub outage](#when-a-hub-goes-offline) visible almost immediately rather than up to two minutes later. Note this speeds up the Cloud Connection entity and the recovery side, not the Repairs notice itself: that notice still waits for three consecutive checks before it appears, deliberately, so a brief blip doesn't flap a card on and off.

### Enabling or disabling push

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. In the **RainPoint Options** form, **Enable push updates** is already checked. Uncheck it to turn push off.
3. Save. The change applies automatically (the integration reloads itself), so you never have to reload or re-add it by hand.

To turn push back on, revisit the same **Configure** screen and check **Enable push updates**.

### Telling whether push is working

Enabling push adds two hub-level diagnostic entities: **`<hub> Push Connected`** (on when the MQTT client is connected) and **`<hub> Push Last Message`** (timestamp of the last message received). If the push connection drops and stays down while polling keeps devices updating, Home Assistant raises a **Settings → Repairs** issue so you know to look. (A channel that stays connected but quietly stops sending updates looks the same as an idle one, so that case is not flagged.)

A separate case is push never starting in the first place: if push is enabled but the integration can't find a usable hub to connect to, it logs a warning and also raises its own **Settings → Repairs** card so you don't have to be watching the log to notice. Polling keeps your devices updating in the meantime. This check only runs when the integration (re)loads, so after fixing the underlying cause, reload the integration or toggle push off and back on to clear the card.

### Notes on push

Push is on by default; if you would rather poll, switch it off on the Configure screen. Turning it off costs you speed, not data: every reading and every hub online or offline change still arrives, just on the 120-second poll cadence instead of within about a second. If the connection ever drops while push is on, Home Assistant reconnects on its own, and the usual 120-second polling keeps your devices up to date in the meantime.

Turning push on doesn't change the [one-session-per-account note above](#use-a-dedicated-home-assistant-account-recommended): it's the sign-in used for setup that can bump your phone out of the app (and vice versa), whether or not push is enabled.

---

## Unverified generic entities (opt-in)

For a device this integration has no tested decoder for, it can fall back to RainPoint's own product catalog and offer provisional entities from it. There are two separate switches, both off by default, and neither affects a device from the [supported list](#supported-devices): those always use their tested decoder.

- **Enable unverified generic sensors** adds provisional readings. They are named with a trailing `(unverified)`, and they are deliberately kept out of long-term statistics, so an unverified number never lands in your energy or history graphs as though it were trustworthy.
- **Enable unverified generic device control** adds controls that open and close real hardware, water valves included. The zone mapping comes from the catalog rather than a tested per-model decoder, so a wrong mapping can run the wrong zone or leave water running. Turn this on only if you are willing to watch what happens the first time.

Both are conservative about what they create. Generic sensors appear for a device only when every reading it reports has a definition the integration recognises, so many devices produce none at all. A generic control never guesses: it shows only state it has read back from the device, never the state you just commanded.

Some device firmwares report their status in a comma-and-semicolon text format rather than the hex format most report in. This is independent of the two toggles above: for a device on that format, the [Raw Payload diagnostic sensor's](#my-device-isnt-listed) decoded fields (and the pre-filled bug report it links to) surface only its signal strength, and unverified generic sensors and control never create working entities for it. The rest of that format is deliberately left unparsed, not missing by accident: it carries its readings by position with no label, each device family orders them differently, and RainPoint's own product data does not record that order, so parsing it would mean guessing, and a guessed reading that looks plausible and is wrong is worse than no reading at all. For the same reason, unverified generic device control will not report such a device as open or closed and will not confirm that a command took effect.

### Enabling or disabling generic entities

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. Check **Enable unverified generic sensors**, **Enable unverified generic device control**, or both.
3. Save. The change applies automatically, so you never have to reload or re-add the integration by hand.

The form tells you how many devices on your account each option would currently affect, so you can see whether turning it on would produce anything at all before you commit to it. Unchecking an option on the same screen turns it back off, and the entities it created are removed rather than left behind unavailable, along with their recorded history.

If a generic reading turns out to be right (or wrong) for your device, that is worth [reporting](#my-device-isnt-listed): it is what turns a catalog guess into a tested decoder.

---

## Attribution

This project is based on [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in homeassistant-homgar.

The original MIT license is preserved. See [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup (venv, Pylance, running tests).
