# Changelog

All notable changes to the RainPoint Cloud integration will be documented in this file.

## [1.3.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.2.0...v1.3.0) (2026-04-16)


### Added

* refresh RainPoint brand assets ([#13](https://github.com/funkadelic/ha-rainpoint/issues/13)) ([47f7959](https://github.com/funkadelic/ha-rainpoint/commit/47f7959a20111e3e8ae1e05fec35c1c36c077131))

## [1.2.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.1.0...v1.2.0) (2026-04-16)


### Added

* publish test coverage improvements from [#7](https://github.com/funkadelic/ha-rainpoint/issues/7) and [#8](https://github.com/funkadelic/ha-rainpoint/issues/8) ([4025735](https://github.com/funkadelic/ha-rainpoint/commit/4025735425fd800c5a4d78df8208baf134cef791))

## [1.1.0](https://github.com/funkadelic/ha-rainpoint/compare/v1.0.0...v1.1.0) (2026-04-14)


### Added

* **02-02:** add test directory structure and seed decoder tests ([8c35ac8](https://github.com/funkadelic/ha-rainpoint/commit/8c35ac83b5a75c095d17326e01425aef53080178))
* bootstrap pytest harness with ruff baseline ([5005498](https://github.com/funkadelic/ha-rainpoint/commit/5005498e3d025cd0bf900c7f2316452ab5d07a66))


### Fixed

* **02:** address PR review findings across 5 files ([b6ab91d](https://github.com/funkadelic/ha-rainpoint/commit/b6ab91df855f002800718244c5555fe86c1b324b))
* **02:** fix CI failures and address code review finding [#2](https://github.com/funkadelic/ha-rainpoint/issues/2) ([8b317bd](https://github.com/funkadelic/ha-rainpoint/commit/8b317bd0f1bdaa5931d4e01ba60886307f889ed2))
* **02:** use %s format for rssi_dbm log statements that may be None ([503a95b](https://github.com/funkadelic/ha-rainpoint/commit/503a95b01571a7af957c91f70c939d73765a6208))
* **02:** WR-01 add pytest-asyncio to requirements-test.txt ([6e82a67](https://github.com/funkadelic/ha-rainpoint/commit/6e82a676cd585000d585aa433a316da79181b6bc))
* **02:** WR-02 add missing HA module stubs to conftest ([76714ee](https://github.com/funkadelic/ha-rainpoint/commit/76714ee7371cb09c1d78fc0551c1170a358551a8))
* **02:** WR-03 warn and return None for non-negative ASCII RSSI values ([7366c33](https://github.com/funkadelic/ha-rainpoint/commit/7366c330207632dad705f56f845f246edbb430cf))
* **02:** WR-04 read tlv directly in zone dict to eliminate stale variable references ([7d2ddac](https://github.com/funkadelic/ha-rainpoint/commit/7d2ddac380b9969f550eabcd20f75ffaac7d90b4))
* **02:** WR-05 return structured dict instead of raising ValueError in reload_service error paths ([84eeaf1](https://github.com/funkadelic/ha-rainpoint/commit/84eeaf13369d91f0fb64ee4bdbfd2a57649a00f3))
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
- WR-05 return structured dict instead of raising ValueError in reload_service error paths
- WR-04 read tlv directly in zone dict to eliminate stale variable references
- WR-03 warn and return None for non-negative ASCII RSSI values
- WR-02 add missing HA module stubs to conftest
- WR-01 add pytest-asyncio to requirements-test.txt

### Changed

- Merge pull request #2 from funkadelic/feat/phase-2-test-harness
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
