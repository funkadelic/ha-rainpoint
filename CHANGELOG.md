# Changelog

All notable changes to the RainPoint Cloud integration will be documented in this file.

## [1.10.1](https://github.com/funkadelic/ha-rainpoint/compare/v1.10.0...v1.10.1) (2026-07-26)


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
