# Contributing

Thanks for your interest in improving `ha-rainpoint`. Bug reports, payload captures for new devices, and PRs are all welcome.

## Development setup

The project targets **Python 3.13** (matches CI).

Install [uv](https://docs.astral.sh/uv/) if you don't have it, then:

```bash
# From the repo root
uv venv                                        # reads .python-version
source .venv/bin/activate
uv pip install -r requirements-test.txt ruff
```

uv installs this dependency tree in a fraction of pip's time (seconds rather than a minute), which is why CI uses it too.

If you'd rather stick with pip, the equivalent is:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt ruff
```

Nothing else in this guide depends on which installer you used.

`requirements-test.txt` pulls in `pytest-homeassistant-custom-component`, which in turn installs a pinned `homeassistant`: do not add `homeassistant` as a separate dependency.

### Editor (VS Code / Pylance)

After creating the venv:

1. `Ctrl+Shift+P` → **Python: Select Interpreter** → pick `.venv/bin/python`.
2. Reload the window. `homeassistant.*` imports will now resolve.

`.venv/` and `.vscode/` are gitignored, so don't commit either.

## Running checks

```bash
pytest              # test suite with coverage
ruff check .        # lint
ruff format .       # format
```

CI runs the same `pytest` invocation plus `hassfest` and HACS validation on every PR. Coverage is uploaded to Codecov and a SonarQube scan runs on PRs (skipped for Dependabot).

Every action in `.github/workflows/` is pinned to a full commit SHA with the version in a trailing comment, e.g. `uses: actions/checkout@3d3c42e... # v7.0.1`. A tag can be repointed at new code without review, so a new `uses:` line needs a SHA rather than `@v4`. Dependabot reads the trailing comment and bumps both parts together. Two actions have no usable release and track a branch commit instead, `hacs/action` and `home-assistant/actions/hassfest`; Dependabot cannot bump those, so refresh them by hand.

## Mutation testing (optional)

```bash
uv pip install --group mutation                                        # or: pip install --group mutation
mutmut run --max-children 4 'custom_components.rainpoint.api.trust.*'  # one module
mutmut results                                                         # what survived
mutmut show MUTANT_NAME                                                # one of those names, and its exact change
```

Scope it to a module while you work on that module. A whole-tree `mutmut run` covers over fifteen thousand mutants and takes hours, though results are cached, so a later run picks up where the last one stopped. `mutants/` is the working copy mutmut builds; it is gitignored and safe to delete.

Pass `--max-children`, and pick a number below your core count. It defaults to one worker per core, and every worker is a forked copy of a process that has already imported Home Assistant and the whole test suite, so a default run saturates the machine and costs a few hundred MB per worker. That is enough to leave a laptop, or a WSL session, unresponsive until the run finishes. Half your cores is a reasonable ceiling, and prefixing the command with `nice -n 19` keeps the rest of your shell usable.

A surviving mutant is a question, not a defect: it names a change to the source that no test objects to. Sometimes that means a missing assertion, sometimes it means the line genuinely doesn't matter.

Configuration lives in `pyproject.toml` under `[tool.mutmut]`, with comments explaining why coverage is switched off for those runs, why one digest-pinning test is deselected, and why editing one of the files the tests open by path throws the cache away rather than reusing it.

## Adding a new device model

Follow the pattern in `custom_components/rainpoint/api/decoders.py`:

1. Capture a raw payload. The quickest source is the device's **Download diagnostics** file, which carries the payload and the integration's decode of it with no setup. The disabled-by-default "Raw Payload" diagnostic sensor exposes the same string; see `DEBUG_VALVE_PAYLOAD.md` for the full capture procedure and for capturing the same device in several states.
2. Add `MODEL_XXX` to `const.py`.
3. Write `decode_xxx(raw: str) -> dict` in `api/decoders.py` and re-export from `api/__init__.py`.
4. Register `MODEL_XXX: decode_xxx` in `DECODER_REGISTRY` in `coordinator.py`.
5. Wire any model-specific entities in `sensor.py` / `binary_sensor.py` / `valve.py` / `number.py`.
6. For valve models, also add `MODEL_XXX` to the `VALVE_MODELS` set in `const.py`. The `valve.py` and `number.py` platforms filter on that set, so a valve model absent from it gets no valve or duration entities. A variant that shares an existing decoder (e.g. `HTV345FRF` reusing `decode_htv213frf_valve`) only needs the registry mapping and `VALVE_MODELS` entry, not a new `decode_xxx`.

Unknown models are handled gracefully by the coordinator, so partial support is fine.

## Versioning

Releases are automated by `release-please`. Do not bump `manifest.json` or `const.VERSION` manually. See `docs/VERSION_ENFORCEMENT.md`.

## Commit and PR style

- Conventional-commit-style **PR titles** (`feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `ci`, `build`, `chore`): enforced on every PR by the `lint-pr-title` workflow. PRs squash-merge into `main`, so the PR title becomes the commit subject that release-please parses; a non-conventional title silently skips the release.
- Keep PRs focused; large multi-concern diffs are harder to review.
