# Version Enforcement

This repository uses [release-please](https://github.com/googleapis/release-please) to automate version bumps, changelog generation, and GitHub Releases.

## Version Locations

The version is tracked in two files:

- `custom_components/rainpoint/manifest.json` — the canonical source (read by HACS)
- `custom_components/rainpoint/const.py` (`VERSION`) — used in runtime logging

Both are updated automatically by release-please when a release PR is merged.

## How Releases Work

1. Push commits to `main` using [conventional commit](https://www.conventionalcommits.org/) prefixes (`feat:`, `fix:`, etc.)
2. Release-please automatically opens (or updates) a release PR titled "chore(main): release X.Y.Z"
3. The PR contains:
   - Version bumps to `manifest.json` and `const.py`
   - An auto-generated changelog entry derived from commit messages
4. Review the PR, edit the changelog if needed, and merge
5. On merge, release-please creates a `vX.Y.Z` tag and a GitHub Release

## Version Bumping Rules

Release-please determines the version bump from commit prefixes:

- `fix:` commits trigger a **patch** bump (1.0.0 -> 1.0.1)
- `feat:` commits trigger a **minor** bump (1.0.0 -> 1.1.0)
- `BREAKING CHANGE:` footer or `!` after type triggers a **major** bump (1.0.0 -> 2.0.0)

## Configuration

- `release-please-config.json` — changelog sections, extra files, release type
- `.release-please-manifest.json` — current version tracker

## Changelog Visibility

| Commit prefix | Changelog section | Visible? |
| --- | --- | --- |
| `feat:` | Added | Yes |
| `fix:` | Fixed | Yes |
| `perf:` | Performance | Yes |
| `refactor:` | Changed | Yes |
| `docs:` | Documentation | No |
| `test:` | Testing | No |
| `ci:`, `build:` | CI / Build | No |
| `chore:` | Miscellaneous | No |

Hidden commits are excluded from user-facing release notes. Edit the release PR body to override visibility for a specific release.
