# Version Enforcement

This repository uses [release-please](https://github.com/googleapis/release-please) to automate version bumps, changelog generation, and GitHub Releases.

## Version Locations

The version is tracked in two files:

- `custom_components/rainpoint/manifest.json`: the canonical source (read by HACS)
- `custom_components/rainpoint/const.py` (`VERSION`): used in runtime logging

Both are updated by release-please in its auto-generated release PR. When that PR is merged, the version bumps land on `main` and trigger tag/release creation.

## How Releases Work

1. Push commits to `main` using [conventional commit](https://www.conventionalcommits.org/) prefixes (`feat:`, `fix:`, etc.)
2. Release-please automatically opens (or updates) a release PR titled "chore(main): release X.Y.Z"
3. The PR contains:
   - Version bumps to `manifest.json` and `const.py`
   - An auto-generated changelog entry derived from commit messages
4. Review the PR, edit the changelog if needed, and merge
5. On merge, release-please creates a `vX.Y.Z` tag and a GitHub Release

## Client Library Pin

The integration's connection code is the `aiorainpoint` package. It is pinned exactly in the `requirements` list of `custom_components/rainpoint/manifest.json` and nowhere else, and CI installs from that line.

Each library release opens a draft `fix(deps): bump aiorainpoint to X.Y.Z` PR from the release bot that changes only that line. Merging it ships the new client in the next release.

The release PR's guard check (`The library pin must be a final release`, in `.github/workflows/release-guard.yml`) fails a stable release whose manifest pins a pre-release, dev or inexact requirement, or pins nothing.

## Pre-releases

Betas come from `prerelease.yml`. They are cut from a feature branch and never committed to main, so a beta may pin whatever its branch pins.

A pre-release must never outlive the stable release of its own version.

Delete it as soon as its purpose ends, and always before the stable release of the same version merges:

```bash
gh release delete <tag> --yes --cleanup-tag
git fetch --prune --prune-tags origin
```

If someone still needs it after that, freeze releases until they are done, or move `last-release-sha` in `release-please-config.json` to the stable release's commit as a temporary bound.

The reason: a beta tag for an already-released version outranks the stable tag while pointing at a commit that is not on main. release-please then finds no stopping point and rebuilds the changelog from the first commit. That happened on 2026-08-17 with `v1.18.0rc3`, and it is what the version check in `release-guard.yml` now turns red.

## Version Bumping Rules

Release-please determines the version bump from commit prefixes:

- `fix:` commits trigger a **patch** bump (1.0.0 -> 1.0.1)
- `feat:` commits trigger a **minor** bump (1.0.0 -> 1.1.0)
- `BREAKING CHANGE:` footer or `!` after type triggers a **major** bump (1.0.0 -> 2.0.0)

## Configuration

- `release-please-config.json`: release type (`simple`, no language-specific publish step), `extra-files` (tells release-please to update `manifest.json` via jsonpath and `const.py` via the `x-release-please-version` annotation), and `changelog-sections` (maps conventional commit types to changelog headings)
- `.release-please-manifest.json`: current version tracker (updated automatically by release-please)

The `const.py` version update relies on the `# x-release-please-version` annotation comment on the `VERSION` line. Do not remove this annotation.

## Changelog Visibility

| Commit prefix | Changelog section | Visible? |
| --- | --- | --- |
| `feat:` | Added | Yes |
| `fix:` | Fixed | Yes |
| `perf:` | Performance | Yes |
| `refactor:` | Changed | Yes |
| `docs:` | Documentation | No |
| `test:` | Testing | No |
| `ci:` | CI | No |
| `build:` | Build | No |
| `chore:` | Miscellaneous | No |

Hidden commits are excluded from user-facing release notes. Edit the release PR body to override visibility for a specific release.
