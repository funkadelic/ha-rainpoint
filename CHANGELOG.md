# Changelog

All notable changes to the RainPoint Cloud integration will be documented in this file.

## [Unreleased]

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
