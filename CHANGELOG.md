# Changelog

All notable changes to the RainPoint Cloud integration will be documented in this file.

## [1.18.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.18.0...v1.18.1) (2026-08-18)


### Fixed

* keep a cloud outage from filling the log and raising a repair ([a234656](https://github.com/funkadelic/ha-rainpoint/commit/a2346561d44751f76419f5a9dd5e32a5c8536270))


### Other Changes

* add a README table of contents and group the sections ([9b4493a](https://github.com/funkadelic/ha-rainpoint/commit/9b4493aac1d4523a32aaba514b4c02b2b294eb79))
* add a test count badge to the README ([#182](https://github.com/funkadelic/ha-rainpoint/issues/182)) ([3d3f3cc](https://github.com/funkadelic/ha-rainpoint/commit/3d3f3cc408c6401ffa6b1a5d9b1c836464200d7d))
* bound the release commit walk at the v1.18.0 release ([#188](https://github.com/funkadelic/ha-rainpoint/issues/188)) ([a0fb855](https://github.com/funkadelic/ha-rainpoint/commit/a0fb85517b3bcdda0394e7161d3068e0e45f87ff))
* bump astral-sh/setup-uv from 9.0.0 to 10.0.1 ([#192](https://github.com/funkadelic/ha-rainpoint/issues/192)) ([341ee20](https://github.com/funkadelic/ha-rainpoint/commit/341ee20946e655403915179e0dce2994a3d66bf9))
* bump schneegans/dynamic-badges-action from 1.7.0 to 1.9.0 ([#193](https://github.com/funkadelic/ha-rainpoint/issues/193)) ([34d7e29](https://github.com/funkadelic/ha-rainpoint/commit/34d7e29e8480bd90c275e68f6f1708ba1ac471e2))
* **catalog:** refresh the product catalog snapshot ([#190](https://github.com/funkadelic/ha-rainpoint/issues/190)) ([f26dbbe](https://github.com/funkadelic/ha-rainpoint/commit/f26dbbebb42d0c4d611a0dd1d12ee994e4ca72fd))
* fail a release PR that moves the version backwards ([4b126dd](https://github.com/funkadelic/ha-rainpoint/commit/4b126ddb3d587b3c572cb347412528ba09b0e56a))
* move the release walk bound to where it is read ([#189](https://github.com/funkadelic/ha-rainpoint/issues/189)) ([92dc1fa](https://github.com/funkadelic/ha-rainpoint/commit/92dc1fa53057ba915c9899ed41299ee077f5cf87))
* open a PR when the product catalog drifts ([#191](https://github.com/funkadelic/ha-rainpoint/issues/191)) ([44af567](https://github.com/funkadelic/ha-rainpoint/commit/44af5673b82aba2f421bca4b08f9a74b34420a5e))
* set up mutation testing with mutmut ([#185](https://github.com/funkadelic/ha-rainpoint/issues/185)) ([c1b0435](https://github.com/funkadelic/ha-rainpoint/commit/c1b04359f7aaedcfa88b90cce8abd240e7a533fe))
* test against current Home Assistant and the supported floor ([#194](https://github.com/funkadelic/ha-rainpoint/issues/194)) ([be17e76](https://github.com/funkadelic/ha-rainpoint/commit/be17e76a5e825e6b3f97539aad3ecf26ac54eae9))

## [1.18.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.17.0...v1.18.0) (2026-08-16)


### What's new

**The HCS008FRF water flow meter now reports real readings**

- If you have one of these meters, every reading on it showed as Unknown. The model was listed as supported, but nothing behind it ever read what the meter sends. It now reports real values, and they match what the RainPoint app shows.
- New: a live **Flow Rate**, in liters per minute.
- Water used and how long the run lasted, for both the run in progress and the last completed one. The two "current" readings sit at zero between runs, matching the "--" the app shows while the meter is idle.
- Water used today and over the meter's lifetime, in liters. The lifetime total can feed Home Assistant's water dashboard.
- Battery, signal strength, firmware version and last updated, as the other devices already have.
- The readings that already existed keep their names and history, so nothing needs re-adding to dashboards or automations.

**The leftover entities card now stays until you answer it**

- The **Settings → Repairs** card that offers to remove a device's leftover entities used to disappear whenever the integration reloaded or Home Assistant restarted, and it never came back. It now stays until you answer it, still scoped to exactly the entities it was raised for.
- Pressing **Submit** while the integration is not running leaves the card in place and tells you to reload first, instead of making it vanish without removing anything.
- Nothing is removed if RainPoint has started listing the device again, including in the first few minutes before it sends a reading.

**A hub that goes offline is reported sooner, and the notice says for how long**

- The notice used to wait for three checks in a row, so an outage shorter than about six minutes was over before it could appear. It now appears once RainPoint has reported the hub offline for three minutes.
- The notice says how long the hub has been offline instead of how many times the integration checked.
- If Home Assistant restarts while a hub is already offline, the notice comes back on the first update rather than starting its wait over.
- The wait is measured against the time RainPoint reports for the outage, so changing the polling interval no longer changes how long the notice takes.

**Debug logs no longer carry identifiers or your email address**

- With debug logging turned on, the log used to contain your account email address, hardware addresses, cloud device identifiers and the names you gave your devices. None of that is written now. Debug logging is not on by default.
- Complete cloud records are replaced by the field names they contained, with no values.
- The device model is still shown, so reporting an unsupported device works exactly as before.

**Also in this release**

- Hubs that fail to answer a poll no longer share one internal marker between them, so a later change cannot make them appear to hold each other's sensors. Nothing about how the integration behaves changes.


### Thanks

Thanks to **@nderooij** for the flow meter payloads, the app screenshots that pinned down the units, and for checking the beta build reading by reading against the app, which is what made this release's flow meter support possible.


### Added

* **repairs:** keep the leftover entities card until you answer it ([#180](https://github.com/funkadelic/ha-rainpoint/issues/180)) ([82c3838](https://github.com/funkadelic/ha-rainpoint/commit/82c3838403d24eeee32c2d2db959f17acd3c4909))
* show real readings for the HCS008FRF flow meter ([#175](https://github.com/funkadelic/ha-rainpoint/issues/175)) ([1d1c377](https://github.com/funkadelic/ha-rainpoint/commit/1d1c377217a4b61c424420dd1f8fba1475779cda))
* show the hub offline notice sooner, and say how long it has been offline ([#178](https://github.com/funkadelic/ha-rainpoint/issues/178)) ([b179c71](https://github.com/funkadelic/ha-rainpoint/commit/b179c71918cc9348bb8d1282db05682c9825324d))


### Fixed

* **logging:** keep device identifiers and email out of debug logs ([#176](https://github.com/funkadelic/ha-rainpoint/issues/176)) ([94cef93](https://github.com/funkadelic/ha-rainpoint/commit/94cef9324fc33bcd52d52bc448ab7dbe6ea92b1a))


### Changed

* guard the absent-hub marker and the generic control card copy ([#177](https://github.com/funkadelic/ha-rainpoint/issues/177)) ([852e56c](https://github.com/funkadelic/ha-rainpoint/commit/852e56ca4a25af54c6737a436cd11404a6523af2))

## [1.17.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.16.0...v1.17.0) (2026-08-14)


### What's new

**Unused entity rows can now be cleaned up**

- When a device stays on your account and reports normally but some of its entity rows sit permanently unavailable, Home Assistant now raises a **Settings → Repairs** card offering to remove them.
- The card lists what it would remove, up to ten entities plus a count of any beyond that, so you can check the list against the device page first. **Cancel** leaves everything alone.
- Watering zones are never offered, so a zone you have not run yet is safe either way. Removing entities also removes the history recorded against them, which cannot be undone.
- Both this card and the existing left-over-device card now name the device and its hub by the names you gave them in Home Assistant, so two cards at once can be told apart without matching an address against a device page.

**Changing a zone's run duration while it is already running now shows an error message**

- Setting a new run duration on a zone that is currently watering is refused with an explanation, instead of appearing to work while the hardware runs to the old value.
- The change is not saved, so set it again once the run ends.
- One quirk worth knowing: the number box may keep showing what you typed until you reload the page. The saved duration and the run in progress are both unaffected.

**Downloaded diagnostics now include your device names**

- A diagnostics file downloaded from the RainPoint entry now opens with a list of your devices, each carrying the name you gave it in Home Assistant.
- Previously the readings in that file were labeled only by the account's internal numbers, so working out which section described which device meant matching those numbers against your device pages by hand.
- Nothing extra is exposed. Your password, tokens, email address and hardware addresses are removed exactly as before.

**Also in this release**

- **Integrations page:** RainPoint now appears under "Services" instead of "Hubs", and the button that adds another account reads "Add service". This is a wording change only, and nothing about your devices or automations changes.
- **README:** the valve hub row now names the per-zone run duration and water used sensors it was missing.


### Added

* include your device names in downloaded diagnostics ([#174](https://github.com/funkadelic/ha-rainpoint/issues/174)) ([ab01a62](https://github.com/funkadelic/ha-rainpoint/commit/ab01a6284264536916324ec7df7da576fb664402))
* **number:** refuse a zone duration change while that zone is watering ([#173](https://github.com/funkadelic/ha-rainpoint/issues/173)) ([1716f1a](https://github.com/funkadelic/ha-rainpoint/commit/1716f1a360116db1decdaccc8c722ee1697a9444))
* **repairs:** prompt to remove unused entities on a working device ([#170](https://github.com/funkadelic/ha-rainpoint/issues/170)) ([6d1930e](https://github.com/funkadelic/ha-rainpoint/commit/6d1930e4b8d32469521252fec2b24d0ac83256f5))

## [1.16.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.15.0...v1.16.0) (2026-08-12)


### What's new

**Every zone now tells you how long it is set to run**

- Each zone on a RainPoint valve hub has a new **Run Duration** reading. While the zone is watering it shows the length of the run, and it drops to zero once the zone closes.
- It is recorded in history like any other sensor, so you can chart how long each zone ran, or use the value in an automation.
- Works on the HTV213, HTV245, HTV345 and HTV405 valve hubs. Nothing about your existing zone entities changes, so no history is lost.

**Support for the HIC801W irrigation controller**

- An HIC801W on your account now appears in Home Assistant and reports what it is doing: which station is watering, how long the run is set for, when it ends, and how many stations the program has finished out of the total.
- Each station also gets its own watering indicator, so you can see at a glance which one is on.
- This release reads the controller only. Starting and stopping it from Home Assistant is not supported yet.

**Also in this release**

- **Sharing one login with the RainPoint app no longer leaves the integration signed out.** RainPoint allows only one active session per account, so if Home Assistant and your phone are using the same credentials, opening the app signs the integration out. Previously it stayed signed out until you stepped in. It now notices and signs itself back in within a few minutes. Setting up a dedicated Home Assistant account, as the README recommends, avoids the situation in the first place and is still the better arrangement.
- **Hub details are filed correctly.** A hub's MAC address now appears as its network connection rather than as a serial number, which is where Home Assistant expects to find it.

### Thanks

Thanks to **@fredclappen** for the HIC801W payload captures and for confirming how the controller behaved during each run, which made this release's support for it possible.



### Added

* add read-only support for the HIC801W irrigation controller ([#163](https://github.com/funkadelic/ha-rainpoint/issues/163)) ([4cd6d68](https://github.com/funkadelic/ha-rainpoint/commit/4cd6d68386dc4c32d0db8e735e7fe13462559cd8))
* **sensor:** surface per-zone run duration on the valve family ([#169](https://github.com/funkadelic/ha-rainpoint/issues/169)) ([c470b3d](https://github.com/funkadelic/ha-rainpoint/commit/c470b3da6478c2534fc2d531c6d568631baac4f5))


### Fixed

* **device:** report the hub MAC as a connection, not a serial number ([#165](https://github.com/funkadelic/ha-rainpoint/issues/165)) ([80e0bd0](https://github.com/funkadelic/ha-rainpoint/commit/80e0bd0ff792773ca1e58e850d557b79331cc2bd))
* recover automatically when another login displaces the session ([b28f4b9](https://github.com/funkadelic/ha-rainpoint/commit/b28f4b991389b784846d9a1c27b5543a92ce2c3c))


### Other Changes

* **catalog:** refresh the product catalog snapshot ([#168](https://github.com/funkadelic/ha-rainpoint/issues/168)) ([6c94dd0](https://github.com/funkadelic/ha-rainpoint/commit/6c94dd07a4a50931d0cd9e794802bc7b640b671d))
* record what the run-state reading is evidenced against ([#167](https://github.com/funkadelic/ha-rainpoint/issues/167)) ([2aae0ba](https://github.com/funkadelic/ha-rainpoint/commit/2aae0ba5d0bc02c53ebbc6a1a1aa7701dd325daa))

## [1.15.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.14.0...v1.15.0) (2026-08-10)


### What's new

**Entity names no longer repeat the device name**

- On a device page, entities now read **Zone 1**, **Battery** and **Moisture Percent** rather than **HTV245FRF Valve Zone 1**, **HTV245FRF Valve Battery** and **HCS026FRF Moisture Percent**. The device name was already at the top of the page, so it was being shown twice and the longer names were getting cut off. Home Assistant still puts the device name in front wherever the device is not obvious, such as the entity list, automations and voice assistants, so nothing is lost anywhere else.
- This is a display change only. No entity ID changes, so automations, scripts and dashboards that refer to an entity by its ID keep working. If you renamed an entity yourself, your name is kept.
- Two smaller effects worth knowing about on upgrade. A device the RainPoint cloud never gave a name to is now shown as `{model} {address}` on its device page, replacing whichever older spelling it happened to register under; if you renamed it yourself, your name is kept. And the **Transmission Power** entity now shows with its device in front of it in the entity list, so two of the same model are no longer both called just "Transmission Power".

**A diagnostics file you can attach to a bug report**

- **Settings → Devices & Services → RainPoint** has a new **Download diagnostics** option in its three-dot menu, and so does every device page. The file describes what the integration last received from RainPoint and what it made of it, including the raw readings and how they were decoded.
- Removed before the file is written: your password, your login tokens, your email address, your hardware's MAC addresses, and the cloud's own device credentials and product keys.
- Kept on purpose: the names of your devices, your hub and your home, the name you gave a device in Home Assistant, and the account-internal numbers that say which device is which. Without those the file cannot tell you which device a section describes. Read it before attaching it to a public issue if any of that is something you would rather not post.
- Only Home Assistant administrators can download it.

**Also in this release**

- **Reporting a problem takes less setup.** There is now a bug report form alongside the existing new-device request, and both lead with the diagnostics file. Filing a useful report no longer means enabling a hidden sensor or turning on debug logging first, and the new-device form no longer insists on a hand-copied payload when you attach a file instead.


### Added

* add a diagnostics download for the config entry and each device ([#158](https://github.com/funkadelic/ha-rainpoint/issues/158)) ([618cf5f](https://github.com/funkadelic/ha-rainpoint/commit/618cf5f8ed1214c3d207e82995af0d1ca929c2e9))
* **diagnostics:** name each device in the diagnostics download ([#162](https://github.com/funkadelic/ha-rainpoint/issues/162)) ([72a63f1](https://github.com/funkadelic/ha-rainpoint/commit/72a63f159a478e1f81951b18eefcd3fcfe5a3945))
* name entities without repeating their device name ([#160](https://github.com/funkadelic/ha-rainpoint/issues/160)) ([a78570c](https://github.com/funkadelic/ha-rainpoint/commit/a78570c38b51801d6be727ce99cd55fa43fb4321))

## [1.14.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.13.1...v1.14.0) (2026-08-07)


### What's new

**Expanded HTV210B support**

- The HTV210B can now be opened and closed from Home Assistant when it is paired through a RainPoint hub. A HTV210B paired over Bluetooth alone cannot be controlled.
- Your existing zone sensors for that valve stay exactly where they are, and you gain per-zone on/off and run duration control. You can now set the transmission power as well: Power Saving, Standard, or Enhance.

**Also in this release**

- **The hub's Automatic Broadcast Time switch actually works now.** It has been on the hub's device page for a while, but it showed a fixed state and flipping it did nothing. It now reads the real setting from RainPoint and changes it there, matching the switch in the app. Next to it is a new **Broadcast Time Now** button that sends the same one-off time broadcast the app's button sends.
- **Push updates are now on for everyone.** Readings arrive as RainPoint sends them rather than waiting for the next scheduled check, which could be a couple of minutes away. Regular checking carries on underneath as a fallback, so nothing breaks if push drops. If you had already turned it on, nothing changes. If you deliberately turned it off, that choice is kept. And if push cannot start, a notice now appears under **Settings → Repairs** instead of the failure only going to the log, so an install can no longer sit quietly without it.
- **A few devices should stop looking wired up but blank.** Some models describe their readings in a format the general-purpose reader did not recognise, and rather than saying so it simply returned nothing, which looked identical to a device with nothing to report. It now reads that format. This only affects the opt-in unverified entities, so it changes nothing unless you have turned those on.


### Added

* add hub broadcast toggle and one-shot button entities ([#156](https://github.com/funkadelic/ha-rainpoint/issues/156)) ([8bd67ec](https://github.com/funkadelic/ha-rainpoint/commit/8bd67ec4aab435f2827d32f1e83d1bc54e3fd13c))
* add transmission power control for sub-device settings ([#157](https://github.com/funkadelic/ha-rainpoint/issues/157)) ([feaf61b](https://github.com/funkadelic/ha-rainpoint/commit/feaf61b09a47cf5f6e8d8ad7b9e6aec7425bb75c))
* add zone valve control for the hub-paired HTV210B ([#150](https://github.com/funkadelic/ha-rainpoint/issues/150)) ([6819474](https://github.com/funkadelic/ha-rainpoint/commit/68194743d0c60f4ad528b48f4f0817c08aae8be2))
* default the push channel on for all installs ([#154](https://github.com/funkadelic/ha-rainpoint/issues/154)) ([a38e938](https://github.com/funkadelic/ha-rainpoint/commit/a38e9381f35746524d10b69257cce7a47c1e3cc0))
* read ASCII-framed headers in the generic decoder ([#155](https://github.com/funkadelic/ha-rainpoint/issues/155)) ([70df62b](https://github.com/funkadelic/ha-rainpoint/commit/70df62b3f1e11e9579c867ff210536ab03d1be78))
* surface push-enable failure as a Repairs card ([#153](https://github.com/funkadelic/ha-rainpoint/issues/153)) ([7d8837a](https://github.com/funkadelic/ha-rainpoint/commit/7d8837a950a7ea680ac0335867810b4af19ce371))


### Fixed

* force both jitter signs in renewal delay test ([#152](https://github.com/funkadelic/ha-rainpoint/issues/152)) ([c022f5b](https://github.com/funkadelic/ha-rainpoint/commit/c022f5b62a9731692deb6e9d9689249c2d2efcd7))

## [1.13.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.13.0...v1.13.1) (2026-08-05)


### What's new

- **A bad reading from RainPoint no longer holds up everything else.** Every couple of minutes the integration asks RainPoint for the latest from your devices. If anything in that answer came back garbled, it used to throw the whole answer away, so every device sat on its old reading until the next check. Now only the garbled part is skipped and the rest arrives as normal. This has not been seen happening to anyone, so treat it as insurance rather than a fix for something you have run into. If it ever does happen, it is noted in the log.


### Fixed

* skip malformed cloud records instead of failing the whole poll ([#147](https://github.com/funkadelic/ha-rainpoint/issues/147)) ([d828093](https://github.com/funkadelic/ha-rainpoint/commit/d8280933e7eed29c0e80d285c2970d21de2b198e))


### Other Changes

* publish the changelog's own section as the release body ([#149](https://github.com/funkadelic/ha-rainpoint/issues/149)) ([4566b68](https://github.com/funkadelic/ha-rainpoint/commit/4566b68a218e5f77e606936f8f508cee0f64a083))

## [1.13.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.12.1...v1.13.0) (2026-08-05)


### What's new

- **Two hubs in one home no longer collide.** Each hub is now identified by your home together with the hub itself, rather than by the home alone. If you have more than one hub, the second hub's sensors, controls and device page now appear properly instead of being dropped. Existing installations are updated automatically the first time this version starts, and each hub keeps its name, its area and its recorded history.
- **Heads up if you ever roll back.** That update to how hubs are stored happens once, on first start, and it is one way. If you later go back to 1.12.x or earlier, the integration will not load at all: Home Assistant reports a migration error and the RainPoint entry stays unavailable until you return to this version or later. Nothing is lost by coming back, but it is worth knowing before you downgrade to chase an unrelated problem.
- **Entities left behind after RainPoint re-lists a device can now be removed.** RainPoint occasionally moves a device to a different parent record on its side, which changes the identity this integration builds its entity IDs from. A fresh set of entities then appears for the same physical device while the old set stays behind, permanently unavailable, so every reading looks like it exists twice. Once a device has been missing from about an hour of checks, a notice appears under **Settings → Repairs** offering to remove the leftover entities. Nothing is removed unless you confirm it, and confirming also deletes the history recorded against those entities, which cannot be undone. If you would rather keep the history, leave the notice alone and the entities stay where they are.


### Added

* key hub identity on the home id and the hub mid ([#143](https://github.com/funkadelic/ha-rainpoint/issues/143)) ([2072505](https://github.com/funkadelic/ha-rainpoint/commit/20725052fc542287a65fcc93114b9e27599ef78a))
* offer leftover entities for removal when a device leaves its hub ([#146](https://github.com/funkadelic/ha-rainpoint/issues/146)) ([5ee1ef9](https://github.com/funkadelic/ha-rainpoint/commit/5ee1ef99b34af11bbfc0ed232cb55b4adfb4d137))


### Other Changes

* bump home-assistant/actions/hassfest ([#145](https://github.com/funkadelic/ha-rainpoint/issues/145)) ([b690a56](https://github.com/funkadelic/ha-rainpoint/commit/b690a563a4d2aaf8c9d0c07f52738980e992489d))
* characterize registry behaviour behind the hub identity re-key ([#141](https://github.com/funkadelic/ha-rainpoint/issues/141)) ([3b87b6f](https://github.com/funkadelic/ha-rainpoint/commit/3b87b6fccdf7b88a87dba65c6c9f6cabe8b48532))
* pre-commit autoupdate ([#144](https://github.com/funkadelic/ha-rainpoint/issues/144)) ([e27d7d0](https://github.com/funkadelic/ha-rainpoint/commit/e27d7d02b0903e6cb8cda9711076983612b6b99c))

## [1.12.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.12.0...v1.12.1) (2026-08-02)


### What's new

- **A "Not Reporting" notice no longer vanishes and comes back for no reason.** When RainPoint returned a device list that was missing a hub it had listed moments earlier, the integration took that at face value: the notices under **Settings → Repairs** for that hub's quiet devices were cleared, then raised again a few minutes later, on a poll where nothing had actually changed. A hub briefly missing from the list is now read as a gap in RainPoint's reporting rather than as news about your devices, so those notices stay put and the hub holds its last known **Cloud Connection** state until the list recovers. A hub that genuinely has been removed still clears its notices as before.


### Fixed

* keep a not-reporting card through a partial device-list outage ([#139](https://github.com/funkadelic/ha-rainpoint/issues/139)) ([163c525](https://github.com/funkadelic/ha-rainpoint/commit/163c525e42ae4ce55f72e7b54a76b8cdabab9d92))

## [1.12.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.11.0...v1.12.0) (2026-08-02)


### What's new

- **You can now see whether your hub is actually reachable.** Each hub gains a **Cloud Connection** entity showing whether RainPoint can currently reach it. When a hub goes offline, its valve controls become unavailable instead of accepting commands that quietly go nowhere, and a notice appears under **Settings → Repairs** naming the hub. This matters because RainPoint keeps serving the last reading it received from an offline hub, so device readings can look current when they are not. The notice and the controls recover on their own once the hub reconnects.
- **Hub connection changes now show up as soon as RainPoint reports them.** Previously the integration only noticed on its next scheduled check, up to a couple of minutes later, in both directions. On installs with push updates enabled, a hub going offline or coming back is reflected immediately. The delay before that point is not something this integration controls: RainPoint itself can take several minutes to notice a hub has gone offline, though it spots a reconnection almost at once.
- **A Bluetooth-only valve is no longer filed under the wrong hub.** If you have a valve paired over Bluetooth rather than to a hub, it was being listed under that hub's **Connected Devices** as though the hub owned it. It now appears as its own device. Existing installs are corrected automatically, though for a device that reports no readings the correction lands a few minutes after the restart rather than immediately.


### Added

* parent a sub-device to the record that carries it ([#138](https://github.com/funkadelic/ha-rainpoint/issues/138)) ([d3d7a93](https://github.com/funkadelic/ha-rainpoint/commit/d3d7a936fe76db8db77dc58d881affa6bf132048))
* surface hub cloud connectivity and gate valve availability on it ([3b50777](https://github.com/funkadelic/ha-rainpoint/commit/3b5077745206453bff08c1d9a29c59ee22d8f497))
* surface hub connectivity at push latency instead of poll latency ([af54c9c](https://github.com/funkadelic/ha-rainpoint/commit/af54c9cc4c1ea8c8014938c3a19dfae16b50877d))


### Other Changes

* retire internal tracker ids, em-dashes, and vendor wording ([#137](https://github.com/funkadelic/ha-rainpoint/issues/137)) ([e87a43a](https://github.com/funkadelic/ha-rainpoint/commit/e87a43a81f389559f1f3dbc32aa1b696f29918bf))

## [1.11.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.10.1...v1.11.0) (2026-07-31)


### What's new

- **A device that reports nothing no longer disappears.** If RainPoint lists a device on your account but never sends readings for it, the integration now says so instead of quietly skipping it. You get a notice under **Settings → Repairs** naming the device, plus a **Not Reporting** entity on the device's own page whose attributes carry a pre-filled report link. Both appear together, about four to six minutes after the device goes quiet, without restarting Home Assistant. This most often means the device is paired over Bluetooth only, is out of range, or is switched off. The notice clears on its own once readings resume, and a hub going offline will not make its healthy devices look silent.
- **More from your valves.** **HTV210B** valves paired to a hub now report battery, signal strength and per-zone state. Valves in the HTV family gained a per-zone water usage reading, and sub-device pages now show firmware version and device ID.
- **Better readings on existing hardware.** Battery levels are now read from the device's own battery datapoint, report times reflect when the device actually measured rather than when it was polled, and signal strength has been restored on valves that had stopped showing it.
- **Unverified generic sensors now cover five curated readings** for devices without tested support here. This is off by default and stays opt-in: turn it on under **Settings → Devices & Services → RainPoint Cloud → Configure**, then **Enable unverified generic sensors**. These readings come from RainPoint's product catalog rather than a tested per-model decoder, so they are labeled unverified and are deliberately kept out of long-term statistics.


### Added

* add HTV210B valve support with per-zone state sensors ([#130](https://github.com/funkadelic/ha-rainpoint/issues/130)) ([161af01](https://github.com/funkadelic/ha-rainpoint/commit/161af01601d342dd1053488c1767f1f1f17790a8))
* add per-zone water usage sensors for the HTV valve family ([#119](https://github.com/funkadelic/ha-rainpoint/issues/119)) ([0128c8d](https://github.com/funkadelic/ha-rainpoint/commit/0128c8da552df54844c95e8e824436bd2bce7099))
* curate five readings for generic sensors, with a record width gate ([#132](https://github.com/funkadelic/ha-rainpoint/issues/132)) ([c014d97](https://github.com/funkadelic/ha-rainpoint/commit/c014d9780308bb07c97e845734f426cb08a4d7b0))
* show firmware version and device id on sub-device pages ([#124](https://github.com/funkadelic/ha-rainpoint/issues/124)) ([e7fe968](https://github.com/funkadelic/ha-rainpoint/commit/e7fe968c067ec5eb8f28ebaedda03cab4f8c28c1))
* surface sub-devices the cloud reports no status for ([#133](https://github.com/funkadelic/ha-rainpoint/issues/133)) ([6e4c522](https://github.com/funkadelic/ha-rainpoint/commit/6e4c522e7ad41519b777b00d920a2cf8471c53c2))


### Fixed

* log the raw value when a push is dropped ([#127](https://github.com/funkadelic/ha-rainpoint/issues/127)) ([8692a80](https://github.com/funkadelic/ha-rainpoint/commit/8692a803163591a3cd8480e47cc6fa7341547c9d))
* narrow the trusted model set to decoders backed by real payloads ([#118](https://github.com/funkadelic/ha-rainpoint/issues/118)) ([515b14a](https://github.com/funkadelic/ha-rainpoint/commit/515b14ab29f41611168b742079d76b3aa235f00b))
* read battery from the STA_BAT datapoint and decode device report time ([#123](https://github.com/funkadelic/ha-rainpoint/issues/123)) ([59be0a2](https://github.com/funkadelic/ha-rainpoint/commit/59be0a23337f5fbfff3ffcf8703b8fe5a3e8ab7e))
* restore missing signal strength readings on some valves ([#129](https://github.com/funkadelic/ha-rainpoint/issues/129)) ([6ead038](https://github.com/funkadelic/ha-rainpoint/commit/6ead0387018e4665a8a51c368af7743f0cf9dc3a))
* stop a Bluetooth parent record from displacing the real hub ([#125](https://github.com/funkadelic/ha-rainpoint/issues/125)) ([9d8dae4](https://github.com/funkadelic/ha-rainpoint/commit/9d8dae4c8e276ad32f93360883e516849f61b1e9))


### Other Changes

* add a hardware trial tool for cloud valve commands and record the Bluetooth valve result ([#131](https://github.com/funkadelic/ha-rainpoint/issues/131)) ([b840654](https://github.com/funkadelic/ha-rainpoint/commit/b8406549aed01fef0d27cce2e1556b8b065a74d1))
* bump actions/labeler from 6.2.0 to 7.0.0 ([#121](https://github.com/funkadelic/ha-rainpoint/issues/121)) ([9e18eb2](https://github.com/funkadelic/ha-rainpoint/commit/9e18eb2705d461580c491e023da291456089909a))
* bump astral-sh/setup-uv from 8.3.2 to 9.0.0 ([#122](https://github.com/funkadelic/ha-rainpoint/issues/122)) ([d8fa7a8](https://github.com/funkadelic/ha-rainpoint/commit/d8fa7a8c028018bedc8cdd4c53cc5b50e27cc8fb))
* record tested device coverage including the HTV210B ([#126](https://github.com/funkadelic/ha-rainpoint/issues/126)) ([791266d](https://github.com/funkadelic/ha-rainpoint/commit/791266dc84216916f55258b9ef6f59603ed3d19a))

## [1.10.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.10.0...v1.10.1) (2026-07-26)


### What's new

- **Changing an option no longer fills the log with errors.** Saving a change under **Settings → Devices & Services → RainPoint Cloud → Configure** reloaded the integration in a way Home Assistant did not recognize, which logged a batch of errors and, in some cases, left the integration failing to come back up until Home Assistant was restarted. Reloading now goes through Home Assistant properly. If you have been seeing "Config entry was never loaded!" in your log, this is the fix.


### Fixed

* reload the entry through Home Assistant instead of unload plus setup ([#116](https://github.com/funkadelic/ha-rainpoint/issues/116)) ([5ca8cb1](https://github.com/funkadelic/ha-rainpoint/commit/5ca8cb1a673b7e3220070aee50d3ffbead41d6a1))

## [1.10.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.9.0...v1.10.0) (2026-07-26)


### What's new

- **Devices this integration doesn't support yet can now show up anyway.** Two new opt-in switches use RainPoint's own product catalog to create provisional sensors, and provisional open/close controls, for hardware that has no tested support here. Both are off by default and everything they create is labeled unverified: readings can be wrong, and a control moves real hardware including water valves, so treat them as a preview rather than something to build automations on. Turn either one on under **Settings → Devices & Services → RainPoint Cloud → Configure**, where the screen also tells you how many of your own devices each switch would actually affect (often zero, which is the honest answer).
- **Battery and signal strength for HTV213 and HTV245 valves.** Each valve now reports its battery level and radio signal, so you can spot one going flat or dropping out of range before it misses a watering.
- **More useful hub information.** The hub now shows its RF channel along with the correct list of channels it actually supports, its WiFi signal strength, and the same Device ID the RainPoint app displays, which makes it far easier to identify the right device when something goes wrong.
- **Own a device this integration doesn't recognize? Two clicks gets it onboarded.** Home Assistant raises a notification naming the unsupported model, with a link that opens a pre-filled GitHub report: the model and its data are already in the form, so you only add what the RainPoint app shows and submit. This release makes that data far more useful, sending named, decoded fields and the product name from RainPoint's catalog instead of a raw hex string. Reports like these are how new hardware gets supported, so please send one if you see the notification.
- **Reminder: real-time updates are available.** Since [1.8.0](https://github.com/funkadelic/ha-rainpoint/releases/tag/v1.8.0) you can optionally get near-instant device updates instead of waiting for the usual two-minute refresh. It's off by default; turn it on under **Settings → Devices & Services → RainPoint Cloud → Configure**. Worth a try if you haven't already.


### Added

* add battery and signal-strength sensors for HTV213/245 valves ([#110](https://github.com/funkadelic/ha-rainpoint/issues/110)) ([d306e6b](https://github.com/funkadelic/ha-rainpoint/commit/d306e6b9d73a22b16ab884b3d1cf38f8b379430c))
* decode unsupported-device payloads for diagnostics ([#102](https://github.com/funkadelic/ha-rainpoint/issues/102)) ([ccea0e1](https://github.com/funkadelic/ha-rainpoint/commit/ccea0e110b726209455d0ffa6866f11e21348cf7))
* enrich unsupported-device diagnostics with a bundled product catalog ([#105](https://github.com/funkadelic/ha-rainpoint/issues/105)) ([4b25518](https://github.com/funkadelic/ha-rainpoint/commit/4b255183a17c812adfa44a87f774888ad64f436c))
* hub diagnostics for RF channel, WiFi RSSI, and device ID ([#111](https://github.com/funkadelic/ha-rainpoint/issues/111)) ([18eb221](https://github.com/funkadelic/ha-rainpoint/commit/18eb221c83c82090754cb4fcfda08ddafcecc99f))
* opt-in generic control entities for catalog-recognized devices ([#109](https://github.com/funkadelic/ha-rainpoint/issues/109)) ([514fa30](https://github.com/funkadelic/ha-rainpoint/commit/514fa30262dbc3839a7b45a908f86ea4d2224001))
* opt-in generic sensor entities for catalog-recognized devices ([#107](https://github.com/funkadelic/ha-rainpoint/issues/107)) ([b25cab3](https://github.com/funkadelic/ha-rainpoint/commit/b25cab3ae5ca171f8f772d960cac872c32982b41))


### Fixed

* name RainPoint instead of "the vendor" in user-facing copy ([#115](https://github.com/funkadelic/ha-rainpoint/issues/115)) ([ad028a4](https://github.com/funkadelic/ha-rainpoint/commit/ad028a4a7632b42ea76195279295e9bc5ef324a6))
* parse the real vendor product-catalog response shape ([#106](https://github.com/funkadelic/ha-rainpoint/issues/106)) ([59b4d53](https://github.com/funkadelic/ha-rainpoint/commit/59b4d53c543f759f111ac495b071aea1ee3d3ccc))
* simplify push toggle description text ([#101](https://github.com/funkadelic/ha-rainpoint/issues/101)) ([5dc64a2](https://github.com/funkadelic/ha-rainpoint/commit/5dc64a2d195be7d31eb8bfeda494a0db8ab848f3))
* split options-flow copy into per-field helper text ([#114](https://github.com/funkadelic/ha-rainpoint/issues/114)) ([ae6c774](https://github.com/funkadelic/ha-rainpoint/commit/ae6c774fabfd0f8a670f1a435ff1358159046e04))


### Performance

* speed up the CI test job ([#112](https://github.com/funkadelic/ha-rainpoint/issues/112)) ([33a51da](https://github.com/funkadelic/ha-rainpoint/commit/33a51dacd530daa2a7ca210c9e8625d0d0c2988d))


### Other Changes

* pin third-party actions to commit SHAs and use uv for local setup ([#113](https://github.com/funkadelic/ha-rainpoint/issues/113)) ([fd59393](https://github.com/funkadelic/ha-rainpoint/commit/fd5939362c594a93a94994cb4ed81c8b92431143))
* repo maintenance ([#108](https://github.com/funkadelic/ha-rainpoint/issues/108)) ([c23f78b](https://github.com/funkadelic/ha-rainpoint/commit/c23f78bc4b95754c9e0b068687002f25bce6ed0a))

## [1.9.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.8.0...v1.9.0) (2026-07-24)


### What's new

- **Two more water timers supported.** The **HTV113FRF** and **HTV145FRF** single-outlet water timers now work in Home Assistant, with open/close control and per-zone run duration, the same as the other RainPoint valves. Both were added from payloads shared by owners of the hardware.
- **Every country in the setup dropdown.** The country picker during setup now lists every country instead of a subset, so more people can sign in without a workaround.
- **One-click reporting for unsupported devices.** When the integration sees a device it doesn't recognize yet, the notification now includes a link that opens a report with the model and raw data already filled in, so helping add support for new hardware takes far less effort.
- **Reminder: real-time updates are available.** Since [1.8.0](https://github.com/funkadelic/ha-rainpoint/releases/tag/v1.8.0) you can optionally get near-instant device updates instead of waiting for the usual two-minute refresh. It's off by default; turn it on under **Settings → Devices & Services → RainPoint Cloud → Configure**. Worth a try if you haven't already.


### Thanks

Thanks to **@blauwaerts** and **@torbertkf** for the HTV145FRF sample payloads, and **@vincentbellizzi-coder** for the HTV113FRF payload, which made this release's new device support possible.


### Added

* add a pre-filled report link to the unsupported-device notification ([#99](https://github.com/funkadelic/ha-rainpoint/issues/99)) ([bb9b8b8](https://github.com/funkadelic/ha-rainpoint/commit/bb9b8b885fa1edd7f153813ea1b82bdebf396522))
* add HTV113FRF single-outlet water timer support ([#98](https://github.com/funkadelic/ha-rainpoint/issues/98)) ([9a724c9](https://github.com/funkadelic/ha-rainpoint/commit/9a724c944beca58d91f6efa54b56adddc8b92805))
* add HTV145FRF single-outlet water timer support ([#74](https://github.com/funkadelic/ha-rainpoint/issues/74)) ([567ee91](https://github.com/funkadelic/ha-rainpoint/commit/567ee91a20801081d4ea66f658c578947b45ab40))
* **config-flow:** cover all countries in the login country picker ([#94](https://github.com/funkadelic/ha-rainpoint/issues/94)) ([682e95c](https://github.com/funkadelic/ha-rainpoint/commit/682e95c3dfe028f3d2f15c7153da94fd8ddad29e))


### Fixed

* use logging.exception on decoder and debug error paths ([#96](https://github.com/funkadelic/ha-rainpoint/issues/96)) ([e2f1573](https://github.com/funkadelic/ha-rainpoint/commit/e2f1573c39a3207137de000fd182bfb0e1295f7c))


### Other Changes

* exclude binary brand assets from Sonar analysis ([#97](https://github.com/funkadelic/ha-rainpoint/issues/97)) ([34d2828](https://github.com/funkadelic/ha-rainpoint/commit/34d2828b5aba3fdafc5a5e16f557e11ba91946c8))
* update README setup steps and contributor guide ([#100](https://github.com/funkadelic/ha-rainpoint/issues/100)) ([121d4d9](https://github.com/funkadelic/ha-rainpoint/commit/121d4d94582e03d11a618bdb38d72c6847023494))

## [1.8.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.7.1...v1.8.0) (2026-07-23)


### What's new

- **Real-time updates (optional, off by default).** Your RainPoint valves, sensors, and other devices can now show changes in Home Assistant almost instantly, instead of waiting for the usual two-minute refresh. It stays optional and is turned off by default. To turn it on: open **Settings → Devices & Services → RainPoint Cloud → Configure**, tick **Enable push updates**, and save. The regular two-minute polling keeps running as a fallback, so nothing breaks if you leave it off or the connection briefly drops.
- **More reliable sign-in.** Resolved a run of recent login and connection failures that could leave the integration stuck as "not ready" or repeatedly retrying. Getting set up and staying connected is now much steadier.


### Added

* add Hungary (+36) to the country code list ([#87](https://github.com/funkadelic/ha-rainpoint/issues/87)) ([aa3a47d](https://github.com/funkadelic/ha-rainpoint/commit/aa3a47de1340cf314775b03419dcfd8ea27d5a85))
* add opt-in MQTT push channel to the RainPoint cloud broker ([#80](https://github.com/funkadelic/ha-rainpoint/issues/80)) ([fed0675](https://github.com/funkadelic/ha-rainpoint/commit/fed0675ee532d117f545ecd1cd5a3f242d59d382))
* deliver device state over the MQTT push channel in near real time ([#83](https://github.com/funkadelic/ha-rainpoint/issues/83)) ([92c3cdf](https://github.com/funkadelic/ha-rainpoint/commit/92c3cdf7e5f05a6367a7925dca82251021101690))
* encrypt the MQTT push channel with TLS ([#85](https://github.com/funkadelic/ha-rainpoint/issues/85)) ([e656068](https://github.com/funkadelic/ha-rainpoint/commit/e656068998b968922af36f3be43d532618bb498d))
* preserve HWS019 daily max/min readings from status payload ([#76](https://github.com/funkadelic/ha-rainpoint/issues/76)) ([736cfff](https://github.com/funkadelic/ha-rainpoint/commit/736cfff001a5a4e6b7e504139b2c2df7ea504f3b))
* report modelCode with unsupported device warnings ([#77](https://github.com/funkadelic/ha-rainpoint/issues/77)) ([f3087c4](https://github.com/funkadelic/ha-rainpoint/commit/f3087c44b0751736a2a37196dca43dfef2b09f00))


### Fixed

* cap login retries with a cooldown after server throttling ([#81](https://github.com/funkadelic/ha-rainpoint/issues/81)) ([31dca9b](https://github.com/funkadelic/ha-rainpoint/commit/31dca9be5db7bc1cbb3bd3dd6e61aa86fc563306))
* keep the MQTT push channel connected by not subscribing ([#92](https://github.com/funkadelic/ha-rainpoint/issues/92)) ([71e3d5c](https://github.com/funkadelic/ha-rainpoint/commit/71e3d5c09700435279a791f9e02de73fc8c50f80))
* re-authenticate when the cloud rejects a stored token as NOT_TOKEN ([#91](https://github.com/funkadelic/ha-rainpoint/issues/91)) ([3f6d635](https://github.com/funkadelic/ha-rainpoint/commit/3f6d6356cdacd720317968c57cc66067e821d22c))
* send an app-like User-Agent so the cloud edge stops returning 403 ([#89](https://github.com/funkadelic/ha-rainpoint/issues/89)) ([e7be9b2](https://github.com/funkadelic/ha-rainpoint/commit/e7be9b2518d9cdf26f5ff6a02a8c538a52247fd6))
* send the full subscribeStatus envelope so the push channel can connect ([#90](https://github.com/funkadelic/ha-rainpoint/issues/90)) ([df4208c](https://github.com/funkadelic/ha-rainpoint/commit/df4208c9c90afad82a913c10dc8be37de22dd821))
* stop hammering the rate-limited login endpoint on setup retries ([#88](https://github.com/funkadelic/ha-rainpoint/issues/88)) ([9e7fb9d](https://github.com/funkadelic/ha-rainpoint/commit/9e7fb9d4c71def98bb33c8934033a9a594cb4b30))


### Other Changes

* bump actions/setup-python from 6 to 7 ([#79](https://github.com/funkadelic/ha-rainpoint/issues/79)) ([2318f7b](https://github.com/funkadelic/ha-rainpoint/commit/2318f7bfe09506dd87c48eb5923bf908b71c863a))
* document the opt-in real-time push channel ([#84](https://github.com/funkadelic/ha-rainpoint/issues/84)) ([cf4fb44](https://github.com/funkadelic/ha-rainpoint/commit/cf4fb44b5042a4794f1e0f81ebe8f92626f47a5e))

## [1.7.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.7.0...v1.7.1) (2026-07-18)


### Other Changes

* add manual pre-release (beta) workflow ([#72](https://github.com/funkadelic/ha-rainpoint/issues/72)) ([33eb9d0](https://github.com/funkadelic/ha-rainpoint/commit/33eb9d0cf3862f823cef5f8d145e2d164a66a633))
* fix homgar domain references and drop unused scripts dir ([#75](https://github.com/funkadelic/ha-rainpoint/issues/75)) ([9c30839](https://github.com/funkadelic/ha-rainpoint/commit/9c30839a66778cf0427199c0419f69f54c51432c))

## [1.7.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.6.0...v1.7.0) (2026-07-18)


### Added

* add HTV405FRF 4-zone valve support ([#70](https://github.com/funkadelic/ha-rainpoint/issues/70)) ([04dd701](https://github.com/funkadelic/ha-rainpoint/commit/04dd701ad877cb3351ed8580df36486c530c47e4))


### Other Changes

* add new device support issue form ([#68](https://github.com/funkadelic/ha-rainpoint/issues/68)) ([e3848a4](https://github.com/funkadelic/ha-rainpoint/commit/e3848a4c53cff556b0cb69c145a47e190a9aa42a))

## [1.6.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.5.3...v1.6.0) (2026-07-17)


### Added

* adding 3 valve HTV345FRF integration; polling fix ([#60](https://github.com/funkadelic/ha-rainpoint/issues/60)) ([3a90bcb](https://github.com/funkadelic/ha-rainpoint/commit/3a90bcb5f06c7f7a27abf3232d925d54e36241fa)) - thanks to first-time contributor @spenceh14 🎉


### Other Changes

* bump actions/checkout from 6 to 7 ([#56](https://github.com/funkadelic/ha-rainpoint/issues/56)) ([9114780](https://github.com/funkadelic/ha-rainpoint/commit/91147804d10eea6140dc196799e49b9c0b9b1592))
* enable pre-commit.ci for automated hook enforcement ([#66](https://github.com/funkadelic/ha-rainpoint/issues/66)) ([6f848e0](https://github.com/funkadelic/ha-rainpoint/commit/6f848e002a1024e777a3255c1d9ce1b971ab3b7e))
* enforce pyproject coverage gate in CI instead of hardcoded 90 ([#62](https://github.com/funkadelic/ha-rainpoint/issues/62)) ([b864c12](https://github.com/funkadelic/ha-rainpoint/commit/b864c12eb014c0a8057c42a927cf11c5f3db902a))
* list HTV345FRF as a supported valve and document VALVE_MODELS registration ([#67](https://github.com/funkadelic/ha-rainpoint/issues/67)) ([4825943](https://github.com/funkadelic/ha-rainpoint/commit/4825943b09c08dd29e9a757b5001b3df519ae596))
* skip SonarQube scan on fork pull requests ([#61](https://github.com/funkadelic/ha-rainpoint/issues/61)) ([9d3d872](https://github.com/funkadelic/ha-rainpoint/commit/9d3d8720c3155cd3756dd88acf07edf601cb1471))

## [1.5.3](https://github.com/funkadelic/ha-rainpoint/compare/v1.5.2...v1.5.3) (2026-06-23)


### Other Changes

* reflect HACS default repository inclusion ([#54](https://github.com/funkadelic/ha-rainpoint/issues/54)) ([22fa0bd](https://github.com/funkadelic/ha-rainpoint/commit/22fa0bdfe1284efb415aaa0518e05626ea4781d4))

## [1.5.2](https://github.com/funkadelic/ha-rainpoint/compare/v1.5.1...v1.5.2) (2026-06-09)


### Other Changes

* bump codecov/codecov-action from 6 to 7 ([#52](https://github.com/funkadelic/ha-rainpoint/issues/52)) ([ab378f4](https://github.com/funkadelic/ha-rainpoint/commit/ab378f4716748cca51e52305737a7a5c32f726de))

## [1.5.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.5.0...v1.5.1) (2026-05-08)


### Other Changes

* cover defensive handlers and raise coverage gate to 99 ([#49](https://github.com/funkadelic/ha-rainpoint/issues/49)) ([c037b96](https://github.com/funkadelic/ha-rainpoint/commit/c037b9660cc613242509dea33875f3af50d46a1f))

## [1.5.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.4.2...v1.5.0) (2026-05-08)


### Changed

* **coordinator:** drop _async_update_data cognitive complexity ([#45](https://github.com/funkadelic/ha-rainpoint/issues/45)) ([aaa984c](https://github.com/funkadelic/ha-rainpoint/commit/aaa984c5a760192564fa58c47f8c34112a9dff46))


### Other Changes

* bump SonarSource/sonarqube-scan-action from 6 to 8 ([#46](https://github.com/funkadelic/ha-rainpoint/issues/46)) ([b2de2a0](https://github.com/funkadelic/ha-rainpoint/commit/b2de2a0c2fa58d8603a084e4116b8e886900fe0e))
* document PR-title gate, Codecov, and SonarQube in CONTRIBUTING ([#43](https://github.com/funkadelic/ha-rainpoint/issues/43)) ([313f277](https://github.com/funkadelic/ha-rainpoint/commit/313f277548db96cd02560aceff963eb1fd66eb16))
* release 1.5.0 ([#48](https://github.com/funkadelic/ha-rainpoint/issues/48)) ([2aa04ab](https://github.com/funkadelic/ha-rainpoint/commit/2aa04abdd428474fbf98af6c74c62ef65d19d54d))

## [1.4.2](https://github.com/funkadelic/ha-rainpoint/compare/v1.4.1...v1.4.2) (2026-04-30)


### Fixed

* **decoders:** route HWS019 payloads missing ';' to error path ([#42](https://github.com/funkadelic/ha-rainpoint/issues/42)) ([5a25231](https://github.com/funkadelic/ha-rainpoint/commit/5a25231b655ab2eb6fd27022c3a912ca1a8f2893))


### Changed

* extract device-id lookup helpers from native_value ([#39](https://github.com/funkadelic/ha-rainpoint/issues/39)) ([426b713](https://github.com/funkadelic/ha-rainpoint/commit/426b7133d01ffa4b695ef0cbd693aa2ec59615b9))
* extract reload-service helpers and normalize response shape ([#34](https://github.com/funkadelic/ha-rainpoint/issues/34)) ([9dbdb49](https://github.com/funkadelic/ha-rainpoint/commit/9dbdb498f17a221e61373b599b84eb4976ef2a82))
* replace sensor setup elif chain with model factory map ([#36](https://github.com/funkadelic/ha-rainpoint/issues/36)) ([0b117fb](https://github.com/funkadelic/ha-rainpoint/commit/0b117fb3150bc2c39bf6a4cbc060f72ecb58d9ec))
* split decode_hws019wrf_v2 into flag/reading helpers ([#40](https://github.com/funkadelic/ha-rainpoint/issues/40)) ([8a15203](https://github.com/funkadelic/ha-rainpoint/commit/8a1520354b0858591920e474b5640f2b818ef0ab))
* split decode_valve_hub into helpers to drop CC under 15 ([#38](https://github.com/funkadelic/ha-rainpoint/issues/38)) ([e02d9db](https://github.com/funkadelic/ha-rainpoint/commit/e02d9db131281bf6fd7ff50f468b8c46b1de67d3))
* split HTV213FRF hex decoder into scan/hub/zone helpers ([#35](https://github.com/funkadelic/ha-rainpoint/issues/35)) ([9b753c5](https://github.com/funkadelic/ha-rainpoint/commit/9b753c5e09b728c40f555cad5a8acac810abb1d4))


### Other Changes

* surface docs, test, ci, build, and chore commits in changelog ([#41](https://github.com/funkadelic/ha-rainpoint/issues/41)) ([833bc11](https://github.com/funkadelic/ha-rainpoint/commit/833bc11fab7e3d944df72303cb047332e0ee1ed0))
* update pytest-cov requirement from &gt;=4.0.0 to &gt;=7.1.0 ([#22](https://github.com/funkadelic/ha-rainpoint/issues/22)) ([a163f01](https://github.com/funkadelic/ha-rainpoint/commit/a163f01e8cf3263fb70aa424064d6bedb43817e9))

## [1.4.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.4.0...v1.4.1) (2026-04-29)


### Fixed

* drop dead conditional in decode_moisture_full status code path ([#29](https://github.com/funkadelic/ha-rainpoint/issues/29)) ([8fb5f66](https://github.com/funkadelic/ha-rainpoint/commit/8fb5f662528118ad56efa89aafe5110abfc351f4))


### Changed

* align return-type hints with actual return values ([#32](https://github.com/funkadelic/ha-rainpoint/issues/32)) ([90d7da1](https://github.com/funkadelic/ha-rainpoint/commit/90d7da1a2bf61cd2c29521310409f9c7fed420e0))
* collapse duplicate HCS sensor-model dispatch branches ([#33](https://github.com/funkadelic/ha-rainpoint/issues/33)) ([b0ec247](https://github.com/funkadelic/ha-rainpoint/commit/b0ec24712c27382619125b242cf5c6d88d38f5c6))
* tighten exception classes and dedupe reload-failure literal ([#31](https://github.com/funkadelic/ha-rainpoint/issues/31)) ([2c1b0fb](https://github.com/funkadelic/ha-rainpoint/commit/2c1b0fbf9e83c4841bd025e7d26f3e1def88184d))

## [1.4.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.3.1...v1.4.0) (2026-04-18)


### Added

* replace country code text input with named dropdown ([#17](https://github.com/funkadelic/ha-rainpoint/issues/17)) ([c4f8dee](https://github.com/funkadelic/ha-rainpoint/commit/c4f8dee35284d1c2ead31d24bb19c47ddaee2b14))

## [1.3.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.3.0...v1.3.1) (2026-04-16)


### Fixed

* package integration contents at zip root for HACS install ([#15](https://github.com/funkadelic/ha-rainpoint/issues/15)) ([f2d62ff](https://github.com/funkadelic/ha-rainpoint/commit/f2d62ff202f35937b4d32951458d3b3457c9ff43))

## [1.3.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.2.0...v1.3.0) (2026-04-16)


### Added

* refresh RainPoint brand assets ([#13](https://github.com/funkadelic/ha-rainpoint/issues/13)) ([47f7959](https://github.com/funkadelic/ha-rainpoint/commit/47f7959a20111e3e8ae1e05fec35c1c36c077131))

## [1.2.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.1.0...v1.2.0) (2026-04-16)


### Added

* publish test coverage improvements from [#7](https://github.com/funkadelic/ha-rainpoint/issues/7) and [#8](https://github.com/funkadelic/ha-rainpoint/issues/8) ([4025735](https://github.com/funkadelic/ha-rainpoint/commit/4025735425fd800c5a4d78df8208baf134cef791))

## [1.1.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.0.0...v1.1.0) (2026-04-14)


### Added

* add test directory structure and seed decoder tests ([8c35ac8](https://github.com/funkadelic/ha-rainpoint/commit/8c35ac83b5a75c095d17326e01425aef53080178))
* bootstrap pytest harness with ruff baseline ([5005498](https://github.com/funkadelic/ha-rainpoint/commit/5005498e3d025cd0bf900c7f2316452ab5d07a66))


### Fixed

* address PR review findings across 5 files ([b6ab91d](https://github.com/funkadelic/ha-rainpoint/commit/b6ab91df855f002800718244c5555fe86c1b324b))
* fix CI failures and address code review finding [#2](https://github.com/funkadelic/ha-rainpoint/issues/2) ([8b317bd](https://github.com/funkadelic/ha-rainpoint/commit/8b317bd0f1bdaa5931d4e01ba60886307f889ed2))
* use %s format for rssi_dbm log statements that may be None ([503a95b](https://github.com/funkadelic/ha-rainpoint/commit/503a95b01571a7af957c91f70c939d73765a6208))
* add pytest-asyncio to requirements-test.txt ([6e82a67](https://github.com/funkadelic/ha-rainpoint/commit/6e82a676cd585000d585aa433a316da79181b6bc))
* add missing HA module stubs to conftest ([76714ee](https://github.com/funkadelic/ha-rainpoint/commit/76714ee7371cb09c1d78fc0551c1170a358551a8))
* warn and return None for non-negative ASCII RSSI values ([7366c33](https://github.com/funkadelic/ha-rainpoint/commit/7366c330207632dad705f56f845f246edbb430cf))
* read tlv directly in zone dict to eliminate stale variable references ([7d2ddac](https://github.com/funkadelic/ha-rainpoint/commit/7d2ddac380b9969f550eabcd20f75ffaac7d90b4))
* return structured dict instead of raising ValueError in reload_service error paths ([84eeaf1](https://github.com/funkadelic/ha-rainpoint/commit/84eeaf13369d91f0fb64ee4bdbfd2a57649a00f3))
* use RELEASE_PLEASE_TOKEN for release asset upload ([be725c0](https://github.com/funkadelic/ha-rainpoint/commit/be725c0b4e4cae2bd6b00a16397a0217b098e296))
* use RELEASE_PLEASE_TOKEN for release asset upload ([dbd6442](https://github.com/funkadelic/ha-rainpoint/commit/dbd6442bae5001919a6520d5a1b23b856d865cbe))

## [Unreleased]

## [1.0.1] - 2026-04-14

### Added

- add test directory structure and seed decoder tests

### Fixed

- fix CI failures and address code review finding #2
- address PR review findings across 5 files
- use %s format for rssi_dbm log statements that may be None
- return structured dict instead of raising ValueError in reload_service error paths
- read tlv directly in zone dict to eliminate stale variable references
- warn and return None for non-negative ASCII RSSI values
- add missing HA module stubs to conftest
- add pytest-asyncio to requirements-test.txt

### Changed

- Merge pull request #2 (test harness)
- clean up upstream leftovers and fix review findings
- add hassfest, HACS, tests, and release workflows from ha-acwd
- run ruff --fix and ruff format to establish clean baseline
- add pyproject.toml, requirements-test.txt, .python-version

## [1.0.0] - 2026-04-12

### Added
- Forked from [homeassistant-homgar](https://github.com/brettmeyerowitz/homeassistant-homgar)
- RainPoint-only integration under the `rainpoint` domain

### Changed
- Renamed integration domain from `homgar` to `rainpoint`
- Removed HomGar/RainPoint dual-brand app-type selection — RainPoint is now the only supported brand
- Hardcoded RainPoint appCode; no user-facing app-type configuration step
- All entity unique IDs use `rainpoint_` prefix
- All class names use `RainPoint` prefix
- Version reset to 1.0.0 for the fork

### Removed
- HomGar app support and dual-brand configuration
- `homgar_api.py` backward-compatibility shim (all imports use `.api` directly)
- `CONF_APP_TYPE`, `APP_CODE_MAPPING`, `BRAND_MAPPING` constants
- Debug worker URL (set to empty string to prevent upstream submission)

### Migration
- This is a fresh-install-only fork. Users migrating from upstream `homeassistant-homgar` must remove the old integration and re-add it as RainPoint Cloud.
