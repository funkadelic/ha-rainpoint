# Contributing

Thanks for your interest in improving `ha-rainpoint`. Bug reports, payload captures for new devices, and PRs are all welcome.

## Development setup

The project targets **Python 3.13** (matches CI).

```bash
# From the repo root
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-test.txt
pip install ruff
```

`requirements-test.txt` pulls in `pytest-homeassistant-custom-component`, which in turn installs a pinned `homeassistant` — do not add `homeassistant` as a separate dependency.

### Editor (VS Code / Pylance)

After creating the venv:

1. `Ctrl+Shift+P` → **Python: Select Interpreter** → pick `.venv/bin/python`.
2. Reload the window. `homeassistant.*` imports will now resolve.

`.venv/` and `.vscode/` are gitignored — don't commit either.

## Running checks

```bash
pytest              # test suite with coverage
ruff check .        # lint
ruff format .       # format
```

CI runs the same `pytest` invocation plus `hassfest` and HACS validation on every PR. Coverage is uploaded to Codecov and a SonarQube scan runs on PRs (skipped for Dependabot).

## Adding a new device model

See the "Adding support for a new device model" section in `CLAUDE.md` (if present) or follow the pattern in `custom_components/rainpoint/api/decoders.py`:

1. Capture a raw payload. The disabled-by-default "Raw Payload" diagnostic sensor exposes it; see `DEBUG_VALVE_PAYLOAD.md` for the full capture procedure.
2. Add `MODEL_XXX` to `const.py`.
3. Write `decode_xxx(raw: str) -> dict` in `api/decoders.py` and re-export from `api/__init__.py`.
4. Register `MODEL_XXX: decode_xxx` in `DECODER_REGISTRY` in `coordinator.py`.
5. Wire any model-specific entities in `sensor.py` / `valve.py` / `number.py`.

Unknown models are handled gracefully by the coordinator, so partial support is fine.

## Versioning

Releases are automated by `release-please`. Do not bump `manifest.json` or `const.VERSION` manually — see `docs/VERSION_ENFORCEMENT.md`.

## Commit and PR style

- Conventional-commit-style **PR titles** (`feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `ci`, `build`, `chore`): enforced on every PR by the `lint-pr-title` workflow. PRs squash-merge into `main`, so the PR title becomes the commit subject that release-please parses; a non-conventional title silently skips the release.
- Keep PRs focused; large multi-concern diffs are harder to review.
