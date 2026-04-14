# Version Enforcement

This repository has automated checks to ensure release tags match the manifest version.

## Version Locations

The version must be consistent across:

- `custom_components/rainpoint/manifest.json` — the canonical source
- `custom_components/rainpoint/const.py` (`VERSION`) — used in runtime logging

## GitHub Actions Checks

### Release Tag Check

**File:** `.github/workflows/enforce-version-tags.yml`

**Triggers:**

- Push of any tag starting with `v*`

**Behavior:**

- Extracts version from tag (removes `v` prefix)
- Compares tag version with manifest version
- Blocks tag creation if versions don't match
- Checks for changelog entry
- Provides detailed fix instructions

**Example Error:**

```text
ERROR: Tag version does not match manifest version!
Tag version: 1.3.6
Manifest version: 1.3.5

To fix this:
1. Update custom_components/rainpoint/manifest.json to version 1.3.6
2. Commit and push the version bump
3. Recreate the tag
```

### Release Workflow

**File:** `.github/workflows/release.yml`

**Triggers:**

- Manual dispatch (`workflow_dispatch`) with a version input

**Behavior:**

- Updates both `manifest.json` and `const.py` to the input version
- Auto-generates a changelog entry from commit messages since the last tag
- Commits the version bump, creates a tag, and publishes a GitHub release

## Safe Tag Creation Script

**File:** `scripts/create-release-tag.sh`

**Usage:**

```bash
# Use manifest version (recommended)
./scripts/create-release-tag.sh

# Use specific version (must match manifest)
./scripts/create-release-tag.sh 1.0.1
```

**Features:**

- Ensures tag version matches manifest version
- Checks working directory is clean
- Prevents duplicate tags
- Verifies changelog entry exists
- Creates and pushes tag safely
- Provides next steps for GitHub release

## Version Bumping Guidelines

### When to Bump Version

- **Patch (1.0.0 → 1.0.1):** Bug fixes, small improvements, decoder fixes
- **Minor (1.0.0 → 1.1.0):** New features, new device support, breaking changes to config
- **Major (1.0.0 → 2.0.0):** Major architectural changes, breaking API changes

### How to Bump Version

1. Edit `custom_components/rainpoint/manifest.json`
2. Update `VERSION` in `custom_components/rainpoint/const.py` to match
3. Update `CHANGELOG.md` with changes
4. Commit and push
5. Create release tag using safe script

### Version Format

- Semantic versioning: `MAJOR.MINOR.PATCH`
- Always increment by 1 for the appropriate level
- No leading zeros (use `1.0.1`, not `1.0.01`)

### Safe Release Process

1. **Make changes** and commit with version bump
2. **Test thoroughly** in development environment
3. **Create release tag** using safe script:

   ```bash
   ./scripts/create-release-tag.sh
   ```

4. **Create GitHub release** at the provided URL
5. **Tag enforcement** will verify version match automatically

### Troubleshooting Tag Issues

If tag enforcement fails:

```bash
# Delete wrong tag
git tag -d v1.0.1
git push origin :refs/tags/v1.0.1

# Fix manifest version and const.py VERSION
# Edit custom_components/rainpoint/manifest.json
# Edit custom_components/rainpoint/const.py

# Create correct tag
./scripts/create-release-tag.sh
```
