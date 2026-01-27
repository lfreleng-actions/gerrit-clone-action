<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>
-->

# Mirror Mode Implementation Notes

## Overview

This document describes the implementation of `--mirror` mode as the default
behavior for the gerrit-clone-action tool. The `--mirror` flag creates bare
Git repositories with complete metadata, including all branches, tags, refs,
and notes.

## Implementation Date

2025-01-24

## Motivation

We needed to ensure that by default, all content cloned by the
gerrit-clone-action and gerrit-clone Python CLI tool contains complete
metadata from remote repositories. We do this by making Git's `--mirror`
option the default for all clone operations.

## Changes Made

### 1. Core Model Changes (`src/gerrit_clone/models.py`)

**Added Field:**

- `mirror: bool = True` - New field in the `Config` class with default
  value `True`

**Validation Logic:**

- Added `__post_init__` validation to check for incompatible options
- When `mirror=True` and user provides `depth`: system sets `depth` to
  `None` with a warning
- When `mirror=True` and user provides `branch`: system sets `branch` to
  `None` with a warning
- These options are mutually exclusive according to Git's constraints

### 2. Worker Module Changes (`src/gerrit_clone/worker.py`)

**Modified Method:** `_build_clone_command()`

**Behavior:**

- **Mirror Mode (default):** Adds `--mirror` flag to git clone command
- **Non-Mirror Mode:** Uses original behavior with `--no-hardlinks`,
  `--quiet`, optional `--depth`, and optional `--branch`

### 3. GitHub Worker Changes (`src/gerrit_clone/github_worker.py`)

**Modified Functions:**

- `_clone_with_git()` - Updated to support `--mirror` flag
- `_clone_with_gh_cli()` - Updated to pass `--mirror` through to gh CLI

**Behavior:** Same branching logic as worker.py - mirror mode uses
`--mirror`, non-mirror mode allows depth/branch options.

### 4. CLI Changes (`src/gerrit_clone/cli.py`)

**Added Option to `clone` Command:**

```python
mirror: bool | None = typer.Option(
    None,
    "--mirror/--no-mirror",
    help=(
        "Use git clone --mirror for complete repository metadata "
        "(all refs, tags, branches). Creates bare repository. "
        "Incompatible with --depth and --branch."
    ),
    envvar="GERRIT_MIRROR",
)
```

**Tri-State Behavior:**

- `None` (default): Uses the value from config file/environment variables,
  or falls back to `Config.mirror=True` default
- `--mirror`: Explicitly enables mirror mode
  (overrides config file/environment)
- `--no-mirror`: Explicitly disables mirror mode
  (overrides config file/environment)

This tri-state design allows CLI flags to override configuration sources
while still respecting config files and environment variables when the
user does not provide a CLI flag.

**Added Option to `mirror` Command:**

```python
mirror: bool = typer.Option(
    True,
    "--mirror/--no-mirror",
    help=(
        "Use git clone --mirror for complete repository metadata "
        "(all refs, tags, branches). Creates bare repository. "
        "Incompatible with --depth and --branch."
    ),
    envvar="GERRIT_MIRROR",
)
```

Note: The `mirror` command defaults to `True` directly since it focuses
specifically on mirror operations.

### 5. Configuration Management Changes (`src/gerrit_clone/config.py`)

**Added to `load_config()` signature:**

- `mirror: bool | None = None` parameter

**Environment Variable Support:**

- Added `GERRIT_MIRROR` environment variable in
  `_load_clone_behavior_env_vars()`

### 6. GitHub Actions Interface (`action.yaml`)

**Added Input:**

```yaml
mirror:
  description: >-
    Use git clone --mirror for complete repository metadata...
  required: false
  default: "true"
```

### 7. Documentation Changes (`README.md`)

- Added "Complete Metadata Cloning" to features list
- Updated clone examples to show `--no-mirror` usage
- Added new section "Understanding Mirror Mode"
- Updated GitHub Action examples

### 8. Test Coverage

**Config Tests (`tests/test_config.py`):**

- `test_load_config_mirror_default_true`
- `test_load_config_mirror_explicit_false`
- `test_load_config_mirror_incompatible_with_depth`
- `test_load_config_mirror_incompatible_with_branch`
- `test_load_config_mirror_from_env`

**Worker Tests (`tests/test_worker.py`):**

- `test_build_clone_command_with_mirror`
- `test_build_clone_command_without_mirror`
- `test_build_clone_command_with_depth_no_mirror`
- `test_build_clone_command_with_branch_no_mirror`

## Usage Examples

### CLI Usage

**Default (Mirror Mode - no flag specified):**

```bash
gerrit-clone clone --host gerrit.example.org
# Creates bare repos with all metadata (uses Config.mirror=True default)
```

**Explicit Mirror Mode:**

```bash
gerrit-clone clone --host gerrit.example.org --mirror
# Explicitly enables mirror mode (overrides config file if present)
```

**Non-Mirror Mode:**

```bash
gerrit-clone clone --host gerrit.example.org --no-mirror
# Creates standard repos with working directory (overrides default/config)
```

**With Config File:**

```bash
# If config.yaml has: mirror: false
gerrit-clone clone --host gerrit.example.org
# Uses config file setting (non-mirror mode)

# Override config file to use mirror mode
gerrit-clone clone --host gerrit.example.org --mirror
```

**With Environment Variable:**

```bash
# Environment variable sets the default
export GERRIT_MIRROR=false
gerrit-clone clone --host gerrit.example.org
# Uses environment setting (non-mirror mode)

# CLI flag overrides environment
gerrit-clone clone --host gerrit.example.org --mirror
```

### GitHub Actions Usage

**Default (Mirror Mode - no input specified):**

```yaml
- uses: lfreleng-actions/gerrit-clone-action@v1
  with:
    host: gerrit.example.org
    # No mirror input = uses Config.mirror=True default
```

**Explicit Mirror Mode:**

```yaml
- uses: lfreleng-actions/gerrit-clone-action@v1
  with:
    host: gerrit.example.org
    mirror: true
```

**Non-Mirror Mode:**

```yaml
- uses: lfreleng-actions/gerrit-clone-action@v1
  with:
    host: gerrit.example.org
    mirror: false
```

## Important Considerations

### Bare Repositories

Mirror mode creates **bare repositories** with no working directory:

- ✅ Perfect for backups and archival
- ✅ Ideal for re-mirroring to other servers
- ✅ Contains complete Git metadata
- ❌ No working tree to browse files

### Incompatibilities

The `--mirror` flag is incompatible with:

- `--depth` (shallow clones)
- `--branch` (specific branch clones)

When you enable mirror mode and provide these options, the system ignores
the incompatible options with a warning.

### Configuration Precedence

The mirror option follows standard configuration precedence:

1. **CLI flags** (highest): `--mirror` or `--no-mirror`
2. **Environment variables**: `GERRIT_MIRROR=true` or `GERRIT_MIRROR=false`
3. **Config file**: `mirror: true` or `mirror: false`
4. **Default** (lowest): `Config.mirror=True`

This allows users to:

- Set defaults in config files or environment variables
- Override on a per-command basis with CLI flags
- Use the sensible default (mirror mode) when they do not specify
  a preference

### Backward Compatibility

This is a **breaking change**. Users who need the old behavior must now
explicitly use `--no-mirror` or set it in their config file/environment.

**Migration Path:**

- Users who want working directories: Add `--no-mirror` flag or set
  `GERRIT_MIRROR=false`
- Users who used `--depth` or `--branch`: Add `--no-mirror` explicitly
  (these are incompatible with mirror mode)
- CI/CD pipelines that expect working directories: Update to use
  `mirror: false` or set environment variable
- Users with config files: Add `mirror: false` to config file if they
  want non-mirror mode by default

## Related Files

**Modified:**

- `src/gerrit_clone/models.py`
- `src/gerrit_clone/worker.py`
- `src/gerrit_clone/github_worker.py`
- `src/gerrit_clone/cli.py`
- `src/gerrit_clone/config.py`
- `action.yaml`
- `README.md`
- `tests/test_config.py`
- `tests/test_worker.py`

**Documentation:**

- This file (`MIRROR_IMPLEMENTATION.md`)
