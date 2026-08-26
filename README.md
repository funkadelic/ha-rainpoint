# RainPoint Cloud

[![Build](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml/badge.svg)](https://github.com/funkadelic/ha-rainpoint/actions/workflows/tests.yml)
[![Tests](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Ffunkadelic%2Fe814bc9b80ce48781f29b860011051d9%2Fraw%2Fha-rainpoint-tests.json)](https://app.codecov.io/gh/funkadelic/ha-rainpoint/tests/main)
[![Codecov](https://img.shields.io/codecov/c/github/funkadelic/ha-rainpoint?logo=codecov)](https://codecov.io/gh/funkadelic/ha-rainpoint)
[![Release](https://img.shields.io/github/release/funkadelic/ha-rainpoint.svg)](https://github.com/funkadelic/ha-rainpoint/releases)
[![License](https://img.shields.io/github/license/funkadelic/ha-rainpoint.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-blue.svg)](https://github.com/hacs/default)

A Home Assistant custom integration for RainPoint Smart+ irrigation devices via the RainPoint cloud API.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=funkadelic&repository=ha-rainpoint&category=integration)

---

## Contents

**Your devices**

- [Supported devices](#supported-devices)
- [My device isn't listed](#my-device-isnt-listed)

**Setting it up**

- [Installation via HACS](#installation-via-hacs)
- [Configuration](#configuration)
- [Use a dedicated Home Assistant account (recommended)](#use-a-dedicated-home-assistant-account-recommended)

**What you get in Home Assistant**

- [Entities](#entities)
- [Transmission power control](#transmission-power-control)

**Optional features**

- [Real-time push updates](#real-time-push-updates)
- [Unverified generic entities (opt-in)](#unverified-generic-entities-opt-in)

**When something looks wrong**

- [When a hub goes offline](#when-a-hub-goes-offline)
- [When a device's entities are left over](#when-a-devices-entities-are-left-over)
- [Downloading diagnostics](#downloading-diagnostics)

**About this project**

- [Attribution](#attribution)
- [Contributing / Issues](#contributing--issues)

---

## Supported devices

This integration supports RainPoint Smart+ device families, including:

| Family | Examples | Entities Created |
| ------ | -------- | ---------------- |
| Valve hubs | HTV245FRF*, HTV113FRF, HTV145FRF, HTV213FRF, HTV345FRF, HTV405FRF, HTV445FRF*, HTV0540FRF | Valve per zone, duration number per zone, run duration sensor per zone, water used sensor per zone |
| Soil sensors | HCS021FRF, HCS026FRF*, HCS005FRF, HCS024FRF-V1 | Moisture, temperature, illuminance |
| Rain sensors | HCS012ARF | Hourly / daily / weekly / total rainfall |
| Temperature & humidity | HCS014ARF | Temperature, humidity |
| Weather stations | HWS019WRF-V2 | Display hub diagnostics |
| Pool sensors | HCS0528ARF*, HCS015ARF | Pool temperature, battery |
| Pool + ambient sensors | HCS015ARF+ | Pool temperature, ambient temperature, humidity |
| CO2 / env sensors | HCS0530THO | CO2, temperature, humidity |
| Flow meters | HCS008FRF* | Live flow rate, water used and run length for the current and last run, water used today and in total, battery, signal strength |
| Bluetooth valves | HTV210B* (hub-paired) | Battery, signal strength, per-zone open/closed state, per-zone open/close control and run duration, transmission power |
| Irrigation controllers | HIC801W* | Valve per station, duration number per station, current watering station, a watering sensor per station, current run length and end time, program stations and stations completed |

\* A model marked with an asterisk has been validated against the hardware itself, by one of three routes: on the maintainer's own devices, by an owner who ran the release and confirmed the readings and controls matched what the device was doing, or by an owner whose diagnostics file let a decoded reading be checked against the value the RainPoint app showed for the same device. Every other model is supported from captured payloads alone, which is enough to decode a reading but not to confirm it against the hardware.

The **HTV210B** only reports to the cloud while paired through a hub. Used over Bluetooth alone, RainPoint still lists it under the hub, but the integration surfaces it as a not-reporting device rather than dropping it silently, since no readings and no control are available in that state. No valve entity is created for it in that state either, so a control that provably cannot reach the hardware is never offered.

The **HIC801W** irrigation controller can start and stop any of its eight stations, alongside everything it already reported. Both halves were confirmed against the hardware by an owner. The controller runs one station at a time and decides that for itself: starting a second station while one is watering sends the command and shows whatever the controller does with it. Run lengths are whole minutes on this model, which is what the controller accepts.

The **HCS008FRF** flow meter reports in liters, matching the RainPoint app. Its lifetime total is the entity to point Home Assistant's water dashboard at, since the meter calibrates that figure itself rather than leaving a pulse count to be converted. The current-run pair reads zero between runs, which is the state the app shows as "--".

Every model listed above has a decoder written against a real payload, and each capability is listed only once it has been confirmed to do what it claims. Support is not claimed optimistically: a control that has not been shown to reach the hardware is not shipped, even where RainPoint's own product data says the device should accept one. If a controller or valve you own is missing here, or is listed without the control you want, contributed payloads are what move it forward. See [My device isn't listed](#my-device-isnt-listed).

A model that is absent is not necessarily unusable: the [opt-in generic entities](#unverified-generic-entities-opt-in) can often surface readings for it from the product catalog, clearly labeled unverified.

All devices communicate via the RainPoint cloud backend. There is no local LAN protocol.

---

## My device isn't listed

There are two different ways a device can look unsupported, and each produces a different surface.

**A device that reports a payload the integration cannot decode.** This doesn't break anything: the integration keeps polling, marks the device as `unknown`, and adds a disabled-by-default **Raw Payload** diagnostic sensor holding its raw data. It also raises a Home Assistant notification with a one-click link that opens a **New device support** report pre-filled with the model and payload.

**A device RainPoint lists on the hub but that returns no readings at all.** There is no payload, so there is no Raw Payload sensor to hold one. Instead, Home Assistant raises a **Settings → Repairs** issue naming the device, and the device gets a single **Not Reporting** diagnostic entity whose state says whether it has never reported or last reported at a given time. That entity's attributes carry the same pre-filled report link, with the payload field stating plainly that the device returns no status. This commonly means the device is paired over Bluetooth only, out of range, or switched off. The integration clears the Repairs issue on its own once readings resume; the Not Reporting entity stays on the device and simply stops reporting a state. The report is still worth filing here: the absence of a payload is itself the finding.

Neither surface is instant. The integration waits until a device has been missing from three consecutive successful checks before treating it as not reporting, which is roughly four to six minutes at the default two-minute polling interval. A check that failed because the hub itself could not be reached does not count against the device, so a hub going offline does not make its healthy children look silent. Once that threshold is crossed, both surfaces appear together in the session that is already running: the Repairs issue is raised and the Not Reporting entity is added at the same point, so a device that goes quiet while Home Assistant is running becomes visible with no further action from you.

To get your device added:

1. Open a [New device support issue](https://github.com/funkadelic/ha-rainpoint/issues/new?template=new_device.yml) (the notification link, or the Not Reporting entity's report link, pre-fills the model and whatever payload is available for you).
2. Include raw payloads in a few known states (valve closed vs open, a sensor at a known reading). One capture shows the byte layout; different states reveal what each byte means. See [`DEBUG_VALVE_PAYLOAD.md`](DEBUG_VALVE_PAYLOAD.md) for how to capture. A device that never reports has no payload to capture; describe what you see in the RainPoint app instead.
3. Attach a diagnostics file, which carries the same information without any capturing on your part. See [Downloading diagnostics](#downloading-diagnostics).

---

## Installation via HACS

This integration is part of the default HACS store, so no custom repository is needed.

The quickest way in is the button at the top of this page, which opens RainPoint Cloud in HACS on your own Home Assistant. From there, click **Download**, restart Home Assistant, then go to **Settings → Devices & Services → Add Integration** and search for **RainPoint Cloud**.

To do it by hand instead:

1. In Home Assistant, open **HACS** from the sidebar.
2. Search for **RainPoint Cloud** and open it.
3. Click **Download** to install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **RainPoint Cloud**.

---

## Configuration

The config flow asks for three fields:

1. **Country**: select the country for your RainPoint account from the dropdown.
2. **Email**: your RainPoint app account email.
3. **Password**: your RainPoint app account password.

After authenticating, select the **home** to monitor. There is no app-type selection; this integration uses the RainPoint app API only.

> **Heads up on API sessions.** The RainPoint cloud allows only one active session per account. Logging in here will sign you out of the RainPoint mobile app on your phone, and vice versa. To avoid that ping-pong, [create a dedicated account for Home Assistant](#use-a-dedicated-home-assistant-account-recommended) and share your home with it. If your phone does sign the integration out, it signs itself back in within a few minutes and carries on, with nothing for you to do.

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
- **Station watering sensors**: one per station on the HIC801W irrigation controller, showing whether that station is currently watering. See [Supported devices](#supported-devices) for the rest of what it reports.
- **Valve entities**: one per irrigation zone, for the valve models listed in the table above, including the HTV210B while it is hub-paired, and one per station on the HIC801W irrigation controller. A device the integration cannot currently reach gets no valve entity, as described under [Supported devices](#supported-devices).
- **Number entities**: one per zone, or per station on the HIC801W, for configuring run duration (1 to 60 minutes), on those same models. The duration applies to the next run: changing it while that zone is already watering is refused with an explanation, and the value you typed is not saved, so set it again once the run ends. A refused change can leave the number box showing what you typed until you reload the page; the saved duration and the run in progress are both unaffected.
- **Hub diagnostic sensors**: RSSI, battery, firmware version, last-data-change timestamp.
  **Last Data Change** is the same value the RainPoint app calls "last acquisition time", and it means what the app says it means: it moves only when the device's readings change or the device restarts. A sensor reporting a steady value produces no change and so no new timestamp, sometimes for many hours, while being perfectly healthy. An unchanged timestamp does not mean the device is offline, and this entity should not be used to decide whether one is. The entity was called **Last Updated** before version 1.21.0; the name changed because it read as "last heard from", which is not what it is. Display only, no entity ID changed and no history was affected.
- **Hub Firmware Update**: one per hub, showing the firmware it is running and whether RainPoint is offering a newer one. The check runs when Home Assistant starts and every six hours after that. Installing is done in the RainPoint app rather than here: the cloud accepts an upgrade request, but it gives no way to choose which version you get and no way to tell a failure from a success, and a firmware update that goes wrong leaves a hub that no longer works. RainPoint's changelog is not shown either, because it comes back in Chinese whatever language is asked for.
- **Hub Cloud Connection**: one binary sensor per hub, on when RainPoint's cloud currently reports that hub as reachable. It exists whether or not [push](#real-time-push-updates) is enabled.
- **Hub Automatic Broadcast Time**: one switch per hub, mirroring the same setting in the RainPoint app.
- **Hub Broadcast Time Now**: one button per hub, sending the same one-shot time broadcast the app's button sends. A press only confirms the cloud accepted the command; there is nothing in the API response to confirm a sub-device actually acted on it.
- **Transmission Power**: one select entity per hub-paired HTV210B. See [Transmission power control](#transmission-power-control) below for what it does and what it cannot confirm.

All entities are grouped under their parent hub device in the Home Assistant device registry.

### Display names on the device page

Entity display names carry only the entity's own label ("Zone 1", "Battery", "Moisture Percent"), and Home Assistant composes the device name in front of it wherever the device is not already obvious. On a device page that means the device name is dropped, so a name no longer truncates in the narrow Controls column. In the entity list, in automations and to voice assistants the full "device plus entity" name still reads as before.

Two upgrade notes, both display only. No entity ID changes and no automation, script or dashboard that refers to an entity by its ID is affected.

- A device the RainPoint cloud never gave a name to is now shown as `{model} {address}` on its device page, replacing whichever of several older spellings it happened to register under. If you renamed the device yourself, your name is kept. If you referred to such a device by name in a template or a `device_attr` lookup, update it to the new name.
- The **Transmission Power** entity now shows with its device in front of it in the entity list, so two of the same model are no longer both called just "Transmission Power".

---

## Transmission power control

A hub-paired HTV210B gets a **Transmission Power** select entity with three options: Power Saving, Standard, and Enhance. It is the only model with this control today. If you have another RainPoint device with a transmission power setting and would like it supported here, please [open an issue](https://github.com/funkadelic/ha-rainpoint/issues) to get it onboarded.

The entity is a configuration control, and it is enabled by default. Nothing inside Home Assistant can confirm a change actually reached the device; only the RainPoint app can, by showing the updated setting there. Check there the first time you change it. If you would rather not have the control at all, disable the entity from the device page or from **Settings → Devices & Services → Entities**, filtered to the device.

---

## Real-time push updates

In addition to the 120-second polling that always runs, the integration surfaces device state changes in near real time over an MQTT push connection. Push is additive and on by default: polling keeps running as the fallback no matter what, so nothing breaks whether push is on, off, or the connection drops. Turning it off is a supported choice, not a workaround.

Poll and push do different jobs, not the same job at different speeds, which is why polling can never be turned off. Every 120 seconds, poll rebuilds the full picture of your account from scratch: it is what notices a device or hub added to the account, and what notices a hub that has quietly left it. Push carries no such information, only state changes for devices poll already knows about, so it can never take over poll's discovery role. Push exists purely to shorten how long a known device's reading, or a known hub's online or offline state, takes to reach Home Assistant, from up to two minutes down to about a second.

Push carries two kinds of update: new readings from individual devices, and a hub going offline or coming back. The second is what makes [a hub outage](#when-a-hub-goes-offline) visible almost immediately rather than up to two minutes later. This speeds up the Cloud Connection entity and the recovery side, not the Repairs notice itself: that notice waits until the hub has been offline for a few minutes, deliberately, so a brief blip doesn't flap a card on and off.

If your account has more than one hub, push covers all of them over the same single connection, and every update is applied to the hub it came from. Hub online and offline changes are confirmed to arrive this way for every hub. Device readings are expected to as well, and are handled the same way when they do, but that has not yet been confirmed on an account with devices paired to a second hub. Either way polling keeps every hub's readings up to date on the 120-second cadence.

### Enabling or disabling push

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. In the **RainPoint Options** form, **Enable push updates** is already checked. Uncheck it to turn push off.
3. Save. The change applies automatically (the integration reloads itself), so you never have to reload or re-add it by hand.

To turn push back on, revisit the same **Configure** screen and check **Enable push updates**.

### Telling whether push is working

Enabling push adds two diagnostic entities to every hub: **`<hub> Push Connected`** (on when the push connection is up) and **`<hub> Push Last Message`** (when that hub last sent something). With more than one hub, Push Connected reads the same on each, because one connection serves them all, while Push Last Message is that hub's own. A hub with nothing paired to it has nothing to send, so its Push Last Message stays blank until that hub next goes offline or comes back; on a hub with devices, a time noticeably older than the rest means that hub has gone quiet. These entities are created when the integration loads, so a hub you add later gets its pair after you reload the integration. If the push connection drops and stays down while polling keeps devices updating, Home Assistant raises a **Settings → Repairs** issue so you know to look. (A channel that stays connected but quietly stops sending updates looks the same as an idle one, so that case is not flagged.)

A separate case is push never starting in the first place: if push is enabled but the integration can't find a usable hub to connect to, it logs a warning and also raises its own **Settings → Repairs** card so you don't have to be watching the log to notice. Polling keeps your devices updating in the meantime. This check only runs when the integration (re)loads, so after fixing the underlying cause, reload the integration or toggle push off and back on to clear the card.

### Notes on push

Push is on by default; if you would rather poll, switch it off on the Configure screen. Turning it off costs you speed, not data: every reading and every hub online or offline change still arrives, just on the 120-second poll cadence instead of within about a second. If the connection ever drops while push is on, Home Assistant reconnects on its own, and the usual 120-second polling keeps your devices up to date in the meantime.

Turning push on doesn't change the [one-session-per-account note above](#use-a-dedicated-home-assistant-account-recommended): it's the sign-in used for setup that can bump your phone out of the app (and vice versa), whether or not push is enabled.

---

## Unverified generic entities (opt-in)

For a device this integration has no tested decoder for, it can fall back to RainPoint's own product catalog and offer provisional entities from it. There are two separate switches, both off by default, and neither affects a device from the [supported list](#supported-devices): those always use their tested decoder.

- **Enable unverified generic sensors** adds provisional readings. They are named with a trailing `(unverified)`, and they are deliberately kept out of long-term statistics, so an unverified number never lands in your energy or history graphs as though it were trustworthy.
- **Enable unverified generic device control** adds controls that open and close real hardware, water valves included. The zone mapping comes from the catalog rather than a tested per-model decoder, so a wrong mapping can run the wrong zone or leave water running. Turn this on only if you are willing to watch what happens the first time.

Both are conservative about what they create. Generic sensors appear for a device only when every reading it reports has a definition the integration recognizes, so many devices produce none at all. A generic control never guesses: it shows only state it has read back from the device, never the state you just commanded.

Some device firmwares report their status in a comma-and-semicolon text format rather than the hex format most report in. This is independent of the two toggles above: for a device on that format, the [Raw Payload diagnostic sensor's](#my-device-isnt-listed) decoded fields (and the pre-filled bug report it links to) surface only its signal strength, and unverified generic sensors and control never create working entities for it. The rest of that format is deliberately left unparsed, not missing by accident: it carries its readings by position with no label, each device family orders them differently, and RainPoint's own product data does not record that order, so parsing it would mean guessing, and a guessed reading that looks plausible and is wrong is worse than no reading at all. For the same reason, unverified generic device control will not report such a device as open or closed and will not confirm that a command took effect.

### Enabling or disabling generic entities

1. Go to **Settings → Devices & Services → RainPoint Cloud → Configure**.
2. Check **Enable unverified generic sensors**, **Enable unverified generic device control**, or both.
3. Save. The change applies automatically, so you never have to reload or re-add the integration by hand.

The form tells you how many devices on your account each option would currently affect, so you can see whether turning it on would produce anything at all before you commit to it. Unchecking an option on the same screen turns it back off, and the entities it created are removed rather than left behind unavailable, along with their recorded history.

If a generic reading turns out to be right (or wrong) for your device, that is worth [reporting](#my-device-isnt-listed): it is what turns a catalog guess into a tested decoder.

---

## When a hub goes offline

Each hub's **Cloud Connection** entity reflects whether RainPoint's cloud currently reports that hub as reachable, refreshed on every poll (every two minutes by default). With [push](#real-time-push-updates) enabled, RainPoint also announces a hub going offline or coming back as it happens, and the entity follows within a second or so instead of waiting for the next poll. The poll keeps running either way, so nothing depends on push being on.

Once a hub has been reported offline for a few minutes, Home Assistant also raises a **Settings → Repairs** notice naming the hub, so a brief blip doesn't flap a card on and off. Valve controls for devices on that hub become unavailable as soon as the hub is reported offline, which is within a single check, or near-immediately with push enabled, since a command cannot reach hardware the cloud itself cannot reach.

Every other reading on that hub keeps showing its last known value. RainPoint keeps serving the last reading it received for every device on an offline hub, for as long as the outage lasts, rather than reporting that a device has gone quiet. That means a reading can look perfectly current in Home Assistant while the hub behind it has actually been offline for hours: the moisture, temperature, or rainfall value shown is the last one RainPoint delivered, not necessarily a fresh one. The integration leaves those readings visible rather than hiding them, because the data catches up once the hub reconnects (within seconds with push enabled, on the next scheduled check without it), and marking devices unavailable for a condition that clears itself would leave gaps in your history and break any automation or template built around a device going offline.

To tell whether what you're looking at is current, check two things: the hub's own **Cloud Connection** entity, and the `hub_connected` attribute Home Assistant now attaches to every entity on that hub's devices. The attribute is always present: `hub_connected` is `true` when the cloud reports the hub connected, `false` when it reports the hub offline, and `none` when the cloud hasn't said either way yet. Test that unknown state with `is none` rather than `is defined`, since the key exists either way. A dashboard card or an automation condition can check this attribute directly to flag a reading that might be stale.

Everything above clears on its own after the hub reconnects: the Repairs notice closes, the Cloud Connection entity turns back on, valve controls return, and no reload is needed. With push enabled that happens within seconds of the hub coming back; otherwise it waits for the next scheduled check, so allow up to two minutes.

---

## When a device's entities are left over

Two different things can leave you with entity rows that will never update again, and each raises its own **Settings → Repairs** card. Both cards name the device and its hub by the names you gave them in Home Assistant, so when two are up at once you can tell them apart without cross-referencing an address against a device page. Anything you have never renamed is named by RainPoint's own string for it instead.

### The device is gone from your account

RainPoint sometimes moves a device to a different parent record on its side, which changes the identity this integration builds its entity IDs from. A fresh set of entities then appears for the same physical device while the old set stays behind, permanently unavailable, so every reading looks like it exists twice.

Home Assistant raises "A device's entities are left over from an older listing" once the old listing has been absent from thirty consecutive checks, roughly an hour at the default two-minute polling interval. The window is deliberately long, and it pauses entirely while the device's hub is itself missing from your account listing, so a cloud-side blip cannot strand a healthy device's entities.

This card keeps until you answer it. Reloading the integration or restarting Home Assistant leaves it where it is, still scoped to exactly the entities it was raised for, so deferring it costs you nothing and you can come back to it whenever you like. It goes on its own only if RainPoint starts listing the old device again, in which case there is nothing left to remove.

### The device is still here, but some of its entities are not

A device can stay on your account and report normally while some of its entity rows go unused: a reading it used to send and no longer does, or an entity a newer version of this integration replaced with a better one. Those rows sit permanently unavailable on an otherwise healthy device page.

Home Assistant raises "A device has unused entities" only once the rows have looked that way on every one of the last thirty checks. Those checks are counted as your devices report rather than on a clock, and with push enabled a check happens whenever a device sends a reading, so the real wait depends on how chatty your devices are. Restarting Home Assistant or reloading the integration starts the count again from zero, and a check that could not run leaves it where it was.

This card names the entities it would remove, up to ten of them and a count of any beyond that, so you can check the list against the device page before deciding. It can still offer a row that is only temporarily quiet, because a reading that has not arrived since the last restart looks the same from inside the integration as one that is gone for good. Two things are never offered here: the outlets a device waters through, whether your model calls them zones or stations, so one you have not run yet is safe either way, and anything on a device that has stopped reporting altogether, which gets its own card saying so instead. **Cancel** leaves everything alone. Unlike the card above, this one is rebuilt rather than kept: reloading the integration or restarting Home Assistant sends it away, and it returns once the rows have looked unused for thirty checks again.

### Neither card removes anything on its own

Short of deleting entities yourself under **Settings → Devices & services → Entities**, those cards' **Submit** buttons are the only thing that removes an entity you did not opt into, and they delete the history recorded against those entities along with them, which cannot be undone. (The one other integration-side deletion is switching off an option you turned on yourself, under [unverified generic entities](#unverified-generic-entities-opt-in), which clears the entities that option created.) If you would rather keep the history, leave the card alone: the leftover entities stay where they are, unavailable but intact. On the first card, the leftover device page is released at the same time as the entities, once it carries nothing else; on the second the device is still in use, so its page stays.

---

## Downloading diagnostics

Home Assistant can write out a diagnostics file describing what this integration last received from RainPoint and what it made of it. It is the single most useful thing to attach to a bug report, and it saves a round trip asking you for details.

Go to **Settings → Devices & Services**, stay on the **Integrations** tab and click the **RainPoint Cloud** card to open it. Your account is the row underneath, which unless you have renamed it reads **RainPoint** followed by your email address in parentheses. Open the three-dot menu on that row and choose **Download diagnostics**. The file covers every device on the account, and its name begins with `config_entry-rainpoint-`.

Every device page carries the same option in its own menu, which is the one to use when only one device is misbehaving. That file is named after the device instead, so the name is also the quickest way to tell the two apart once they are in your downloads folder.

The file opens with a list of your devices, each carrying the name you gave it in Home Assistant, so you can tell which section describes which device.

Removed before the file is written: your password, your login tokens, your email address, your hardware's MAC addresses, and the cloud's own device credentials and product keys. Kept on purpose: the names of your devices, your hub and your home as they appear in the app, the name you gave a device in Home Assistant, and the account-internal numbers this integration uses to tell one device from another. Nothing in that second list authenticates anything, and without it the file cannot say which device a section describes, which is the only reason to download it. Read the file before you attach it to a public issue if any of that is something you would rather not post. Only Home Assistant administrators can download it.

---

## Attribution

This project is based on [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar) by Brett Meyerowitz.

Special thanks to [shaundekok/rainpoint](https://github.com/shaundekok/rainpoint) for payload decoding inspiration referenced in homeassistant-homgar.

The original MIT license is preserved. See [LICENSE](LICENSE).

---

## Contributing / Issues

Report bugs and request features at: <https://github.com/funkadelic/ha-rainpoint/issues>

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup (venv, Pylance, running tests).
