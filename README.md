<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>
-->

# 🔄 Gerrit Clone

A production-ready multi-threaded CLI tool and GitHub Action for bulk cloning
repositories from Gerrit servers with automatic API discovery. Designed for
reliability, speed, and CI/CD compatibility.

## Features

- **Automatic API Discovery**: Discovers Gerrit API endpoints across different
  server configurations (`/r`, `/gerrit`, `/infra`, etc.)
- **Bulk Repository Discovery**: Fetches all projects via Gerrit REST API with
  intelligent filtering
- **Multi-threaded Cloning**: Concurrent operations with auto-scaling thread
  pools (up to 32 workers for Gerrit, 64 for GitHub with 2x multiplier for
  network-limited operations)
- **Hierarchy Preservation**: Maintains complete Gerrit project folder
  structure without flattening
- **Complete Metadata Cloning**: Uses `git clone --mirror` by default to
  capture all refs, tags, and branches for complete repository backups
- **GitHub Mirroring**: Mirror Gerrit repositories to GitHub organizations
  with automatic name transformation
- **Robust Retry Logic**: Exponential backoff with jitter for transient
  network and server failures
- **SSH Integration**: Full SSH agent, identity file, and config support
- **CI/CD Ready**: Non-interactive operation with structured JSON manifests
- **Smart Filtering**: Automatically excludes system repos and archived
  projects
- **Rich Progress Display**: Beautiful terminal progress bars with per-repo
  status tracking
- **Comprehensive Logging**: Structured logging with configurable verbosity
  levels

## Installation

### Using uvx (Recommended)

For one-time execution without installation:

```bash
uvx gerrit-clone --host gerrit.example.org
```

### Using uv

```bash
uv tool install gerrit-clone
./gerrit-clone --host gerrit.example.org
```

### From Source

```bash
git clone https://github.com/lfreleng-actions/gerrit-clone-action.git
cd gerrit-clone-action
uv sync
uv run gerrit-clone --host gerrit.example.org
```

## CLI Usage

### Commands

The tool provides five main commands:

- **`clone`**: Clone all repositories from a Gerrit server
- **`refresh`**: Refresh local content cloned from a Gerrit server
- **`mirror`**: Mirror repositories from a Gerrit server to GitHub
- **`reset`**: Remove all repositories from a GitHub organization
- **`config`**: Show effective configuration from all sources

### Clone Command Examples

Clone all active repositories from a Gerrit server:

```bash
gerrit-clone clone --host gerrit.example.org
```

Clone to a specific directory with custom thread count:

```bash
gerrit-clone clone --host gerrit.example.org \
  --output-path ./repositories \
  --threads 8
```

Clone with shallow depth and specific branch (requires --no-mirror):

```bash
gerrit-clone clone --host gerrit.example.org \
  --no-mirror \
  --depth 10 \
  --branch main \
  --threads 16
```

Clone without mirror mode (standard clone with working directory):

```bash
gerrit-clone clone --host gerrit.example.org \
  --no-mirror
```

Include archived repositories and use custom SSH key:

```bash
gerrit-clone clone --host gerrit.example.org \
  --include-archived \
  --ssh-user myuser \
  --ssh-private-key ~/.ssh/gerrit_rsa
```

### Understanding Path Behavior

By default (when `--output-path` is not specified), `gerrit-clone`
automatically creates an organized folder structure to prevent naming
conflicts when cloning from different sources:

**Gerrit sources:**

```bash
# Clones to: ./gerrit.example.org/project-name
gerrit-clone clone --host gerrit.example.org

# Strips port from output-path: ./gerrit.example.org/project-name
gerrit-clone clone --host gerrit.example.org:29418
```

**GitHub sources:**

```bash
# Clones to: ./github.com/myorg/repo-name
gerrit-clone clone --host github.com/myorg

# GitHub Enterprise: ./github.enterprise.com/engineering/repo-name
gerrit-clone clone --host github.enterprise.com/engineering
```

**Custom path (no auto-structure):**

```bash
# Clones directly to: ./my-repos/project-name
gerrit-clone clone --host gerrit.example.org --output-path ./my-repos
```

This design prevents repository naming conflicts when running concurrent
clone operations in the same directory (e.g., on GitHub Actions runners).

### Understanding Mirror Mode

By default, `gerrit-clone` uses `git clone --mirror` to create **bare repositories**
that contain complete metadata from the remote repository:

- ✅ All branches (not just the default)
- ✅ All tags
- ✅ All refs and notes
- ✅ Complete commit history
- ✅ Perfect for backups and mirrors

**Important**: Mirror mode creates **bare repositories** (no working directory).
This is ideal for:

- Creating complete backups
- Mirroring to other Git servers (e.g., GitHub)
- Archival purposes
- CI/CD pipelines that only need repository metadata

**When to use `--no-mirror`**:

- You need a working directory to browse/edit files
- You only want a specific branch (`--branch`)
- You want a shallow clone (`--depth`)

**Note**: The `--mirror` flag is incompatible with `--depth` and `--branch`.
If you specify mirror mode along with these options, the tool ignores them with
a warning.

### Refresh Command Examples

Refresh all repositories in the current directory:

```bash
gerrit-clone refresh
```

Refresh ONAP repositories in a specific directory:

```bash
gerrit-clone refresh --output-path ~/repos/onap
```

Fetch only (don't merge changes):

```bash
gerrit-clone refresh --output-path ~/onap --fetch-only
```

Use 16 threads for faster refresh:

```bash
gerrit-clone refresh --output-path ~/onap --threads 16
```

Automatically stash uncommitted changes before refresh:

```bash
gerrit-clone refresh --output-path ~/onap --auto-stash
```

Use rebase strategy instead of merge:

```bash
gerrit-clone refresh --output-path ~/onap --strategy rebase
```

Dry run to see what changes would occur:

```bash
gerrit-clone refresh --output-path ~/onap --dry-run
```

Refresh all repositories (not just Gerrit):

```bash
gerrit-clone refresh --all-repos
```

### Complete Refresh Example

Here's a complete example showing the refresh workflow:

```bash
# Refresh ONAP repositories with auto-stash and 16 threads
gerrit-clone refresh \
  --output-path ~/repos/onap \
  --threads 16 \
  --auto-stash \
  --strategy merge
```

**Expected Output**:

```text
🏷️  gerrit-clone refresh version X.Y.Z

Refresh Configuration
Base Path: ~/repos/onap
Threads: 16
Mode: Pull (merge)
Prune: True
Timeout: 300s
Skip Conflicts: True
Auto Stash: True
Filter: Gerrit only
Dry Run: False
Force: False
Recursive: True

🔍 Discovering Git repositories in ~/repos/onap
📂 Discovered 127 Git repositories
🔄 Refreshing 127 repositories with 16 threads

Refreshing repositories ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 127/127 • 0:02:15

Refresh Summary
────────────────────────────────────────────────────────────
Total Repositories: 127
Duration: 135.4s

Results:
  ✅ Successful: 115
  ✓  Up-to-date: 98
  🔄 Updated: 17
  ❌ Failed: 2
  ⊘  Skipped: 8
  ⚠️  Conflicts: 2

Repositories Updated: 17
Total Files Changed: 156

Issues:
  ❌ ccsdk-apps-services: Network error during pull
  ⚠️  ccsdk-features-sdnr: Merge conflicts detected

📄 Manifest: ~/repos/onap/refresh-manifest.json

⚠️  2 repositories failed to refresh
```

**Key Features**:

- **Parallel Updates**: Uses concurrent threads for fast updates
- **Smart Detection**: Automatically detects Gerrit repositories by remote URL
- **Safe Defaults**: Skips repositories with uncommitted changes by default
- **Auto-Stash**: Optionally stash and restore uncommitted changes
- **Flexible Strategies**: Support for merge (fast-forward) or rebase
- **Detailed Reporting**: JSON manifest with complete results
- **Dry Run**: Preview changes without applying them

### Mirror Command Examples

Mirror all repositories from Gerrit to a GitHub organization:

```bash
gerrit-clone mirror --server gerrit.onap.org --org modeseven-onap
```

Mirror specific projects with hierarchical filtering:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --output-path /tmp/modeseven-onap \
  --projects "ccsdk, oom, cps"
```

Use HTTP API for discovery and HTTPS for cloning (no SSH required):

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --discovery-method http \
  --https
```

Include archived/read-only repositories:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --include-archived
```

Delete and recreate existing GitHub repositories and overwrite local clones:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --recreate \
  --overwrite
```

**Note on Project Hierarchies**: When specifying projects like `ccsdk`, all
sub-projects (e.g., `ccsdk/apps`, `ccsdk/features`) are automatically
included. GitHub repository names replace slashes with hyphens:
`ccsdk/features/test` becomes `ccsdk-features-test`.

**GitHub Authentication**: Set the `GITHUB_TOKEN` environment variable with a
personal access token that has the required permissions:

```bash
export GITHUB_TOKEN=github_pat_your_token_here
gerrit-clone mirror --server gerrit.example.org --org your-org
```

**Token Requirements**:

- **Classic Token**: Scopes: `repo`, `delete_repo`
- **Fine-grained Token**: Permissions:
  - Contents (Read and Write)
  - Administration (Read and Write)
  - Metadata (Read access, automatic)

### Complete Mirror Example

Here's a complete example showing the full workflow with expected output:

```bash
# Set GitHub token (get from https://github.com/settings/tokens)
# Required scopes: repo, delete_repo
export GITHUB_TOKEN="github_pat_XXXXXXXX"

# Mirror specific projects with recreation
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --output-path /tmp/modeseven-onap \
  --projects "ccsdk, oom, cps" \
  --recreate \
  --overwrite
```

**Expected Output**:

```text
🏷️  gerrit-clone mirror version X.Y.Z

🔑 Authenticating with GitHub...
✓ Using specified organization: modeseven-onap
📋 Project filters: ccsdk, oom, cps
🌐 Connecting to Gerrit: gerrit.onap.org
🔍 Discovering projects on gerrit.onap.org [SSH]
✅ Found 393 projects to process
📦 Found 19 projects to mirror

🚀 Starting mirror operation...

✓ Manifest written to: /tmp/modeseven-onap/mirror-manifest.json

Mirror Summary
  Total: 19
  Succeeded: 19
  Failed: 0
  Skipped: 0
  Duration: 231.9s
```

The `--recreate` flag deletes and recreates existing GitHub repositories for a
clean mirror. Use `--overwrite` to also re-clone from Gerrit. See the manifest
file for detailed per-repository results.

### Reset Command Examples

Remove all repositories from a GitHub organization (with safety prompts):

```bash
# By default, excludes automation PRs from counts
gerrit-clone reset --org my-test-org
```

Include automation PRs (dependabot, pre-commit.ci, etc.) in counts:

```bash
gerrit-clone reset --org my-test-org --include-automation-prs
```

Compare local Gerrit clone with remote GitHub before deletion:

```bash
gerrit-clone reset --org my-test-org \
  --output-path /tmp/gerrit-mirror \
  --compare
```

Delete immediately without confirmation (DANGEROUS!):

```bash
gerrit-clone reset --org my-test-org --no-confirm
```

**⚠️ WARNING: This operation is DESTRUCTIVE and IRREVERSIBLE!**

The `reset` command will:

1. List all repositories in the organization with PR/issue counts
2. Optionally compare with local Gerrit clone (`--compare` flag)
3. Prompt for unique confirmation hash (unless `--no-confirm`)
4. Permanently delete all repositories

**Safety Features:**

- Displays comprehensive table with repo names, open PRs, and open issues
- **Excludes automation PRs by default** (dependabot, pre-commit.ci,
  renovate, github-actions, allcontributors)
- Requires unique hash based on organization state (prevents accidental
  deletion)
- Optional local/remote synchronization comparison
- Clear warnings about data loss

**GitHub Token Requirements:**

The `reset` command requires a **Classic Personal Access Token** with the
following scopes:

- `repo` (full control of private repositories)
- `delete_repo` (delete repositories)
- `read:org` (read organization data)

**⚠️ Important:** Fine-grained tokens are generally NOT supported for the
`reset` command because they may not provide the required repository
permissions (including deletion) across targeted repositories and
organizations. Use a classic Personal Access Token with the scopes below.

To create a classic token:

1. Go to <https://github.com/settings/tokens>
2. Click "Generate new token" → "Generate new token (classic)"
3. Select the required scopes: `repo`, `delete_repo`, `read:org`
4. Set a token expiry date
5. Copy the token and set it as an environment variable:

```bash
export GITHUB_TOKEN="ghp_your_classic_token_here"
gerrit-clone reset --org my-test-org
```

### Reset Command Options

**Required:**

- `--org TEXT`: GitHub organization to reset (delete all repositories)

**Optional:**

- `--output-path DIRECTORY`: Local Gerrit clone folder hierarchy (default: current
  directory)
- `--compare`: Compare local Gerrit clone with remote GitHub repositories
  before deletion
- `--github-token TEXT`: GitHub personal access token (or use
  `GITHUB_TOKEN` env var)
- `--include-automation-prs`: Include automation PRs (dependabot,
  pre-commit.ci, etc.) in PR counts (default: excluded)
- `--no-confirm`: Skip confirmation prompt and delete immediately
- `--verbose` / `-v`: Enable verbose output

> **Note:** By default, the tool excludes automation PRs from tools like
> dependabot, pre-commit.ci, renovate, github-actions, and allcontributors.
> Use `--include-automation-prs` to include them.

### Clone Command Options

```text
Usage: gerrit-clone clone [OPTIONS]

Options:
  -h, --host TEXT                 Gerrit server hostname [required]
  -p, --port INTEGER              Gerrit SSH port [default: 29418]
  --base-url TEXT                 Base URL for Gerrit API
  -u, --ssh-user TEXT             SSH username for clone operations
  -i, --ssh-private-key PATH      SSH private key file for authentication
  --output-path PATH              Base directory for clone hierarchy [default: .]
  --skip-archived / --include-archived
                                  Skip archived and inactive repositories
                                  [default: skip-archived]
  --include-project TEXT          Restrict cloning to specific project(s)
  --discovery-method TEXT         Method for discovering projects: ssh (default),
                                  http (REST API), or both (union of both methods
                                  with SSH metadata preferred)
  --ssh-debug                     Enable verbose SSH (-vvv) for troubleshooting
  --allow-nested-git / --no-allow-nested-git
                                  Allow nested git working trees when cloning
                                  both parent and child repositories
                                  [default: allow-nested-git]
  --nested-protection / --no-nested-protection
                                  Auto-add nested child repo paths to parent
                                  .git/info/exclude [default: nested-protection]
  --move-conflicting / --no-move-conflicting
                                  Move conflicting files/directories in parent
                                  repos to [NAME].parent to allow nested cloning
  -t, --threads INTEGER           Number of concurrent clone threads
                                  (auto-detected: 2x multiplier for GitHub sources)
  -d, --depth INTEGER             Create shallow clone with given depth
  -b, --branch TEXT               Clone specific branch instead of default
  --https / --ssh                 Use HTTPS for cloning [default: ssh]
  --keep-remote-protocol          Keep original clone protocol for remote
  --strict-host / --accept-unknown-host
                                  SSH strict host key checking [default: strict-host]
  --clone-timeout INTEGER         Timeout per clone operation in seconds
                                  [default: 600]
  --retry-attempts INTEGER        Max retry attempts per repository
                                  [default: 3]
  --retry-base-delay FLOAT        Base delay for retry backoff in seconds
                                  [default: 2.0]
  --retry-factor FLOAT            Exponential backoff factor [default: 2.0]
  --retry-max-delay FLOAT         Max retry delay in seconds [default: 30.0]
  --manifest-filename TEXT        Output manifest filename
                                  [default: clone-manifest.json]
  -c, --config-file PATH          Configuration file path (YAML or JSON)
  --cleanup / --no-cleanup        Remove cloned repositories (path) after
                                  run completes (success or failure)
  --exit-on-error                 Exit when first error occurs
  --log-file PATH                 Custom log file path
  --disable-log-file              Disable creation of log file
  --log-level TEXT                File logging level [default: DEBUG]
  -v, --verbose                   Enable verbose/debug output
  -q, --quiet                     Suppress all output except errors
  --version                       Show version information
  --help                          Show this message and exit
```

### Mirror Command Options

**Discovery and Connection:**

- `--server TEXT`: Gerrit server hostname (required)
- `--port INTEGER`: Gerrit SSH port (default: 29418)
- `--ssh-user TEXT` / `-u`: SSH username for Gerrit clone operations (env: `GERRIT_SSH_USER`)
- `--ssh-private-key PATH` / `-i`: SSH private key file for authentication
  (env: `GERRIT_SSH_PRIVATE_KEY`)
- `--discovery-method [ssh|http|both]`: Method for discovering projects
  - `ssh` (default): Use SSH to query projects (requires SSH access)
  - `http`: Use REST API (no SSH required; recommended for CI/CD)
  - `both`: Union of both methods with SSH metadata preferred
- `--https` / `--ssh`: Use HTTPS for cloning instead of SSH
  (default: SSH)
- `--strict-host` / `--accept-unknown-host`: SSH strict host key checking
  (default: strict)
- `--skip-archived` / `--include-archived`: Skip archived/read-only
  repositories (default: skip)

**GitHub Configuration:**

- `--org TEXT`: Target GitHub organization (required or uses default from token)
- `--github-token TEXT`: GitHub PAT (or use `GITHUB_TOKEN` env var)
- `--recreate`: Delete and recreate existing GitHub repositories
- `--overwrite`: Overwrite local Git repositories at target path

**Project Filtering:**

- `--projects TEXT`: Comma-separated list of project hierarchies to mirror

**Performance:**

- `--threads INTEGER`: Number of concurrent operations (default: auto)
- `--output-path PATH`: Local filesystem path for cloned projects (default: `/tmp/gerrit-mirror`)

**Output:**

- `--manifest-filename TEXT`: Output manifest filename (default: `mirror-manifest.json`)
- `--verbose` / `-v`: Enable verbose/debug output
- `--quiet` / `-q`: Suppress all output except errors

### Environment Variables

You can configure all CLI options through environment variables with
`GERRIT_` prefix:

```bash
export GERRIT_HOST=gerrit.example.org
export GERRIT_PORT=29418
export GERRIT_SSH_USER=myuser
export GERRIT_SSH_PRIVATE_KEY=~/.ssh/gerrit_key
export OUTPUT_PATH=/workspace/repos
export GERRIT_SKIP_ARCHIVED=1
export GERRIT_THREADS=16
export GERRIT_CLONE_DEPTH=5
export GERRIT_BRANCH=main
export GERRIT_STRICT_HOST=1
export GERRIT_CLONE_TIMEOUT=300
export GERRIT_RETRY_ATTEMPTS=5
export GERRIT_DISCOVERY_METHOD=ssh
export GERRIT_CLEANUP=0

gerrit-clone  # Uses environment variables
```

### Using .netrc Files

Gerrit-clone supports loading HTTP credentials from `.netrc` files, following
the standard format used by curl and other tools.

**Search order:**

1. `.netrc` in the current directory
2. `~/.netrc` in your home directory
3. `~/_netrc` (Windows fallback)

**Example `.netrc` file:**

```text
machine gerrit.onap.org login myuser password mytoken
machine gerrit.opendaylight.org login myuser password anothertoken
```

**CLI options:**

| Option                 | Description                                     |
| ---------------------- | ----------------------------------------------- |
| `--http-user TEXT`     | HTTP username for Gerrit authentication         |
| `--http-password TEXT` | HTTP password for Gerrit authentication         |
| `--no-netrc`           | Disable .netrc file lookup                      |
| `--netrc-file PATH`    | Use a specific .netrc file                      |
| `--netrc-optional`     | Do not fail if .netrc file is missing (default) |
| `--netrc-required`     | Require a .netrc file and fail if missing       |

By default, `.netrc` lookup is optional (`--netrc-optional`): if the tool
finds no `.netrc` file, it continues and falls back to environment variables.
Use `--netrc-required` to enforce that a `.netrc` file must be present.

When a `.netrc` file is present, credentials load automatically. Explicit
environment variables or CLI arguments take precedence over `.netrc` entries.

**Credential Priority Order:**

1. **CLI arguments** (`--http-user`/`--http-password`) - highest priority
2. **`.netrc` file** (if not disabled with `--no-netrc`)
3. **Environment variables** (`GERRIT_HTTP_USER`, `GERRIT_HTTP_PASSWORD`)
4. **Fallback environment variables** (`GERRIT_USERNAME`, `GERRIT_PASSWORD`)

The fallback environment variables (`GERRIT_USERNAME`/`GERRIT_PASSWORD`) provide
compatibility with other Gerrit-related actions and tools in the lfreleng-actions
ecosystem.

**Example with explicit credentials:**

```bash
# Use CLI credentials (highest priority)
gerrit-clone clone --host gerrit.example.org --https \
  --http-user myuser --http-password mytoken

# Use .netrc file
gerrit-clone clone --host gerrit.example.org --https \
  --netrc-file ~/.netrc.gerrit

# Require .netrc credentials (fail if not found)
gerrit-clone clone --host gerrit.example.org --https --netrc-required
```

### Configuration Files

Create `~/.config/gerrit-clone/config.yaml`:

```yaml
host: gerrit.example.org
port: 29418
ssh_user: myuser
ssh_identity_file: ~/.ssh/gerrit_key
output-path: /workspace/repos
skip_archived: true
threads: 8
clone_timeout: 600
retry_attempts: 3
retry_base_delay: 2.0
```

Or JSON format `~/.config/gerrit-clone/config.json`:

```json
{
  "host": "gerrit.example.org",
  "port": 29418,
  "ssh_user": "myuser",
  "ssh_identity_file": "~/.ssh/gerrit_key",
  "path": "/workspace/repos",
  "skip_archived": true,
  "threads": 8
}
```

Configuration precedence: CLI arguments > Environment variables > Config file >
Defaults

## GitHub Mirroring

The `mirror` command enables synchronizing Gerrit repositories to GitHub,
transforming hierarchical project structures into GitHub-compatible
repository names.

### How It Works

1. **Discovery**: Discovers projects from the specified Gerrit server
2. **Filtering**: Optionally filters to specific project hierarchies
3. **Name Transformation**: Converts Gerrit paths to GitHub names
   (`ccsdk/features` → `ccsdk-features`)
4. **Cloning**: Creates local mirror clones from Gerrit
5. **GitHub Creation**: Creates repositories in the target GitHub organization
6. **Pushing**: Pushes complete repository history to GitHub

### Hierarchical Project Filtering

When you specify a project name like `ccsdk`, the mirror command
automatically includes all sub-projects in the hierarchy:

- `ccsdk` (exact match)
- `ccsdk/apps` (child)
- `ccsdk/features` (child)
- `ccsdk/features/test` (grandchild)

This hierarchical filtering ensures the tool mirrors complete project
families together.

### Name Transformation

GitHub does not support forward slashes in repository names, so the tool
transforms Gerrit project paths:

| Gerrit Project Name   | GitHub Repository Name |
| --------------------- | ---------------------- |
| `ccsdk`               | `ccsdk`                |
| `ccsdk/apps`          | `ccsdk-apps`           |
| `ccsdk/features`      | `ccsdk-features`       |
| `ccsdk/features/test` | `ccsdk-features-test`  |

### Authentication

GitHub mirroring requires a personal access token with appropriate
permissions:

1. Create a token at <https://github.com/settings/tokens>
2. Required permissions:

   **For Classic Tokens**:
   - `repo` (full control of private repositories)
   - `delete_repo` (delete repositories) - **Required for `--recreate`**
   - `admin:org` (for organization repos) - Optional but recommended

   **For Fine-grained Tokens** (Recommended):
   - Contents: **Read and Write**
   - Administration: **Read and Write** - **Required for `--recreate`**
   - Metadata: **Read access** (automatically included)

3. Set the `GITHUB_TOKEN` environment variable:

```bash
export GITHUB_TOKEN=github_pat_your_token_here
```

### Mirror Behavior

**Default Behavior** (without flags):

- Skips GitHub repositories that already exist
- Skips local repositories that already exist
- Creates new GitHub repositories as needed
- Clones from Gerrit if local path does not exist

**With `--recreate` flag**:

- Deletes and recreates existing GitHub repositories (fresh start)
- Still skips local clones if they exist

**With `--overwrite` flag**:

- Removes and re-clones local repositories
- Still skips existing GitHub repositories unless `--recreate` is also used

**With both `--recreate` and `--overwrite`**:

- Removes and re-clones all local repositories
- Deletes and recreates all GitHub repositories (complete refresh)

### Examples

Basic mirror to an organization:

```bash
export GITHUB_TOKEN=ghp_your_token_here
gerrit-clone mirror --server gerrit.onap.org --org modeseven-onap
```

Mirror specific projects with custom local output-path:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --output-path /tmp/modeseven-onap \
  --projects "ccsdk, oom, cps"
```

Force-update all existing repositories:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --recreate \
  --overwrite
```

Mirror using custom SSH settings:

```bash
gerrit-clone mirror \
  --server gerrit.onap.org \
  --org modeseven-onap \
  --ssh-user myuser \
  --ssh-private-key ~/.ssh/gerrit_key \
  --threads 8
```

### Mirror Output Manifest

The mirror command generates a JSON manifest file
(`mirror-manifest.json` by default) with complete operation details:

```json
{
  "version": "1.0",
  "generated_at": "2025-01-15T10:30:00Z",
  "github_org": "modeseven-onap",
  "gerrit_host": "gerrit.onap.org",
  "total": 150,
  "succeeded": 148,
  "failed": 0,
  "skipped": 2,
  "duration_s": 1234.5,
  "results": [
    {
      "gerrit_project": "ccsdk/apps",
      "github_name": "ccsdk-apps",
      "github_url": "https://github.com/modeseven-onap/ccsdk-apps",
      "status": "success",
      "local_path": "/tmp/modeseven-onap/ccsdk/apps",
      "duration_s": 12.3,
      "attempts": 1
    }
  ]
}
```

## Nested Repository Support

Gerrit Clone includes intelligent support for nested repositories (projects with
hierarchical names like `parent/child`):

### Automatic Detection

- **Dependency Ordering**: The tool automatically clones parent repositories
  before their children
- **Conflict Detection**: Identifies when parent repo content conflicts with
  nested directory structure
- **Smart Batching**: Uses dependency-aware batching to prevent race conditions

### Conflict Resolution Options

#### Skip Conflicting

```bash
gerrit-clone clone --host gerrit.example.org --no-move-conflicting
```

Skips nested repositories when parent contains conflicting files/directories.
Provides clear warnings about skipped repos.

#### Move Conflicting (Default - Recommended for Data Mining)

```bash
gerrit-clone clone --host gerrit.example.org
```

Automatically moves conflicting content in parent repositories to
`[NAME].parent` to allow complete nested cloning. This ensures **100%
repository availability** for reporting and analysis purposes.

**Example:**

- Parent repo `test` contains file named `test`
- Child repo `test/test` needs directory `test/`
- With move-conflicting enabled (default): File `test` → `test.parent`,
  directory created for child repo
- Result: The tool clones both repositories with complete history preservation

### Configuration

```bash
# Allow nested repositories (default: true)
--allow-nested-git / --no-allow-nested-git

# Protect parent repos by adding child paths to .git/info/exclude (default: true)
--nested-protection / --no-nested-protection

# Move conflicting content to allow complete cloning (default: true)
--move-conflicting / --no-move-conflicting
```

## GitHub Action Usage

### Basic Example

```yaml
name: Clone Gerrit Repositories
on: [push]

jobs:
  clone:
    runs-on: ubuntu-latest
    steps:
      - name: Clone repositories
        uses: lfreleng-actions/gerrit-clone-action@v1
        with:
          host: gerrit.example.org
          ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}
          output-path: repositories
          mirror: true  # Default: creates bare repos with complete metadata
```

### Advanced Example

```yaml
name: Clone and Process Repositories
on:
  schedule:
    - cron: '0 2 * * *'  # Daily at 2 AM

jobs:
  clone:
    runs-on: ubuntu-latest
    steps:
      - name: Clone repositories
        id: clone
        uses: lfreleng-actions/gerrit-clone-action@v1
        with:
          host: gerrit.example.org
          port: 29418
          base-url: https://gerrit.example.org/gerrit
          ssh-user: automation
          ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}
          output-path: workspace
          skip-archived: true
          threads: 12
          mirror: false  # Use --no-mirror for working directory
          depth: 1
          branch: main
          use-https: false
          keep-remote-protocol: false
          clone-timeout: 900
          retry-attempts: 5
          verbose: true

      - name: Show results
        run: |
          echo "Total: ${{ steps.clone.outputs.total-count }}"
          echo "Success: ${{ steps.clone.outputs.success-count }}"
          echo "Failed: ${{ steps.clone.outputs.failure-count }}"
          echo "Manifest: ${{ steps.clone.outputs.manifest-path }}"

      - name: Upload manifest
        uses: actions/upload-artifact@v4
        with:
          name: clone-manifest
          output-path: ${{ steps.clone.outputs.manifest-path }}
```

### HTTPS Cloning Example

```yaml
name: Clone via HTTPS
on: [push]

jobs:
  clone:
    runs-on: ubuntu-latest
    steps:
      - name: Clone repositories using HTTPS
        uses: lfreleng-actions/gerrit-clone-action@v1
        with:
          host: gerrit.example.org
          base-url: https://gerrit.example.org/r
          use-https: true
          mirror: true  # Complete mirror with all refs
          output-path: repos
          quiet: true
        env:
          # Use GitHub token or other auth for HTTPS
          GIT_ASKPASS: echo
          GIT_USERNAME: ${{ secrets.GERRIT_USERNAME }}
          GIT_PASSWORD: ${{ secrets.GERRIT_TOKEN }}
```

> **Note:** When using `use-https: true`, the action automatically sets
> `discovery-method: http` to use the Gerrit REST API for project discovery.
> This is necessary because SSH discovery (the default) requires SSH access on
> port 29418, which is not available when using HTTPS. You can override this
> behavior by explicitly setting the `discovery-method` input.

### Nested Repositories with Conflict Resolution

<!-- markdownlint-disable MD013 -->

```yaml
name: Complete Repository Mining
on: [workflow_dispatch]

jobs:
  clone:
    runs-on: ubuntu-latest
    steps:
      - name: Clone all repositories (including nested with conflicts)
        uses: lfreleng-actions/gerrit-clone-action@v1
        with:
          host: gerrit.example.org
          use-https: true
          allow-nested-git: true
          nested-protection: true
          move-conflicting: true  # Move conflicting files to ensure 100% clone success
          output-path: complete-clone
          threads: 8
          verbose: true

      - name: Verify complete data availability
        run: |
          echo "Cloned: ${{ steps.clone.outputs.success-count }}"
          echo "Total repositories: ${{ steps.clone.outputs.total-count }}"
          success_count=${{ steps.clone.outputs.success-count }}
          total_count=${{ steps.clone.outputs.total-count }}
          success_rate=$(( success_count * 100 / total_count ))
          echo "Success rate: ${success_rate}%"

          # Count moved conflicts
          find complete-clone -name "*.parent" | wc -l | xargs echo "Conflicts resolved:"
```

<!-- markdownlint-enable MD013 -->

### Configuration File Example

```yaml
name: Clone with Config File
on: [workflow_dispatch]

jobs:
  clone:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout config
        uses: actions/checkout@v4

      - name: Clone repositories
        uses: lfreleng-actions/gerrit-clone-action@v1
        with:
          config-file: .gerrit-clone-config.yaml
          verbose: true
```

### Action Inputs

<!-- markdownlint-disable MD013 -->

| Input                  | Required | Default               | Description                                                                                          |
| ---------------------- | -------- | --------------------- | ---------------------------------------------------------------------------------------------------- |
| `host`                 | Yes      |                       | Gerrit server hostname                                                                               |
| `port`                 | No       | `29418`               | Gerrit SSH port                                                                                      |
| `base-url`             | No       |                       | Base URL for Gerrit API (defaults to <https://HOST>)                                                 |
| `ssh-user`             | No       |                       | SSH username for clone operations                                                                    |
| `ssh-private-key`      | No       |                       | SSH private key content for authentication                                                           |
| `output-path`          | No       | `.`                   | Clone destination (auto-creates `./{SERVER}/` or `./github.com/{ORG}/` structure when not specified) |
| `skip-archived`        | No       | `true`                | Skip archived and inactive repositories                                                              |
| `include-project`      | No       |                       | Restrict cloning to specific project(s) (comma-separated)                                            |
| `ssh-debug`            | No       | `false`               | Enable verbose SSH (-vvv) for troubleshooting                                                        |
| `allow-nested-git`     | No       | `true`                | Allow nested git working trees                                                                       |
| `nested-protection`    | No       | `true`                | Auto-add nested child repo paths to parent .git/info/exclude                                         |
| `move-conflicting`     | No       | `false`               | Move conflicting files/directories in parent repos to [NAME].parent                                  |
| `exit-on-error`        | No       | `false`               | Exit when first error occurs                                                                         |
| `threads`              | No       | auto                  | Number of concurrent clone threads                                                                   |
| `depth`                | No       |                       | Create shallow clone with given depth                                                                |
| `branch`               | No       |                       | Clone specific branch instead of default                                                             |
| `use-https`            | No       | `false`               | Use HTTPS for cloning instead of SSH                                                                 |
| `keep-remote-protocol` | No       | `false`               | Keep original clone protocol for remote                                                              |
| `strict-host`          | No       | `true`                | SSH strict host key checking                                                                         |
| `clone-timeout`        | No       | `600`                 | Timeout per clone operation in seconds                                                               |
| `retry-attempts`       | No       | `3`                   | Max retry attempts per repository                                                                    |
| `retry-base-delay`     | No       | `2.0`                 | Base delay for retry backoff in seconds                                                              |
| `retry-factor`         | No       | `2.0`                 | Exponential backoff factor for retries                                                               |
| `retry-max-delay`      | No       | `30.0`                | Max retry delay in seconds                                                                           |
| `manifest-filename`    | No       | `clone-manifest.json` | Output manifest filename                                                                             |
| `config-file`          | No       |                       | Configuration file path (YAML or JSON)                                                               |
| `verbose`              | No       | `false`               | Enable verbose/debug output                                                                          |
| `quiet`                | No       | `false`               | Suppress all output except errors                                                                    |
| `log-file`             | No       |                       | Custom log file path                                                                                 |
| `disable-log-file`     | No       | `false`               | Disable creation of log file                                                                         |
| `log-level`            | No       | `DEBUG`               | File logging level                                                                                   |

<!-- markdownlint-enable MD013 -->

### Action Outputs

| Output          | Description                               |
| --------------- | ----------------------------------------- |
| `manifest-path` | Path to the generated clone manifest file |
| `success-count` | Number of cloned repositories             |
| `failure-count` | Number of failed clone attempts           |
| `total-count`   | Total number of repositories processed    |

## SSH Configuration

The tool provides comprehensive SSH authentication support with automatic
configuration detection:

### SSH Authentication Options

The following SSH authentication options are available across all interfaces:

<!-- markdownlint-disable MD013 -->

| Option     | CLI             | Environment              | Action                      | Description  |
| ---------- | --------------- | ------------------------ | --------------------------- | ------------ |
| SSH User   | `-u`            | `GERRIT_SSH_USER`        | `ssh-user`                  | SSH username |
| SSH Key    | `-i` (file)     | `GERRIT_SSH_PRIVATE_KEY` | `ssh-private-key` (content) | Private key  |
| Host Check | `--strict-host` | `GERRIT_STRICT_HOST`     | `strict-host`               | Key check    |

<!-- markdownlint-enable MD013 -->

### Authentication Methods

Three authentication methods provide automatic fallback:

1. **SSH Agent (Recommended)**: Uses keys loaded into SSH agent with automatic
   detection
2. **Identity File**: Explicitly specified private key files with permission
   validation
3. **SSH Config**: Host-specific configuration from ~/.ssh/config with full
   option support

### SSH Setup Examples

#### Using SSH Agent (Recommended for development)

1. Generate SSH key pair:

   ```bash
   ssh-keygen -t ed25519 -C "your.email@example.com"
   ```

2. Add public key to Gerrit profile

3. Add private key to SSH agent:

   ```bash
   ssh-add ~/.ssh/id_ed25519
   ```

4. Clone with agent authentication:

   ```bash
   gerrit-clone clone --host gerrit.example.org --ssh-user myuser
   ```

#### Using SSH Identity File (Recommended for CI/CD)

1. Place private key file securely (e.g., `/path/to/private_key`)

2. Set proper permissions:

   ```bash
   chmod 600 /path/to/private_key
   ```

3. Clone with identity file:

   ```bash
   gerrit-clone clone --host gerrit.example.org \
     --ssh-user myuser \
     --ssh-private-key /path/to/private_key
   ```

4. Or use environment variables:

   ```bash
   export GERRIT_SSH_USER=myuser
   export GERRIT_SSH_PRIVATE_KEY=/path/to/private_key
   gerrit-clone clone --host gerrit.example.org
   ```

### SSH Config

Create `~/.ssh/config` entries for convenience:

```text
Host gerrit.example.org
    User myusername
    IdentityFile ~/.ssh/gerrit_key
    StrictHostKeyChecking yes
```

### Known Hosts

Pre-populate known hosts to avoid prompts (recommended for CI/CD):

```bash
ssh-keyscan -H -p 29418 gerrit.example.org >> ~/.ssh/known_hosts
```

Test SSH connectivity before cloning:

```bash
ssh -p 29418 myuser@gerrit.example.org gerrit version
```

## Output Manifest

Each run generates a detailed JSON manifest (`clone-manifest.json`):

```json
{
  "version": "1.0",
  "generated_at": "2025-01-15T10:30:45Z",
  "host": "gerrit.example.org",
  "port": 29418,
  "total": 42,
  "succeeded": 154,
  "failed": 2,
  "skipped": 0,
  "success_rate": 98.7,
  "duration_s": 89.3,
  "config": {
    "skip_archived": true,
    "threads": 8,
    "depth": null,
    "branch": null,
    "strict_host_checking": true,
    "path": "/workspace/repos"
  },
  "results": [
    {
      "project": "core/api",
      "path": "core/api",
      "status": "success",
      "attempts": 1,
      "duration_s": 3.42,
      "error": null,
      "started_at": "2025-01-15T10:30:15Z",
      "completed_at": "2025-01-15T10:30:18Z"
    },
    {
      "project": "tools/legacy",
      "path": "tools/legacy",
      "status": "failed",
      "attempts": 3,
      "duration_s": 15.8,
      "error": "timeout after 600s",
      "started_at": "2025-01-15T10:30:20Z",
      "completed_at": "2025-01-15T10:30:36Z"
    }
  ]
}
```

## Error Handling

### Common Issues

#### Host key verification failed

```bash
# Accept new host keys (use with caution)
gerrit-clone --host gerrit.example.org --accept-unknown-host

# Recommended: Pre-populate known_hosts
ssh-keyscan -H -p 29418 gerrit.example.org >> ~/.ssh/known_hosts
```

#### Permission denied (publickey)

- Verify SSH public key exists in Gerrit profile
- Check SSH agent has key loaded: `ssh-add -l`
- Test SSH connection: `ssh -p 29418 username@gerrit.example.org gerrit version`
- Verify SSH key permissions: `chmod 600 ~/.ssh/id_rsa`

#### Connection timeout or network errors

- Verify Gerrit server hostname and port (often 29418 for SSH)
- Check network connectivity and firewall rules
- Increase timeout: `--clone-timeout 900`
- Reduce concurrency: `--threads 4`

#### Path conflicts or permission errors

- Existing non-git directories block clones
- Use clean target directory: `--output-path ./clean-workspace`
- Check disk space and write permissions
- Remove conflicting directories: `rm -rf conflicting-path`

#### API discovery failures

- Manually specify base URL: `--base-url https://host/gerrit`
- Verify Gerrit server is accessible via HTTPS
- Check for corporate proxy or firewall restrictions

### Exit Codes

The tool uses standardized exit codes for different failure types, making
it easier to handle errors in automation and CI/CD pipelines:

<!-- markdownlint-disable MD013 -->

| Exit Code | Description             | Common Causes                  | Resolution            |
| --------- | ----------------------- | ------------------------------ | --------------------- |
| **0**     | Success                 | All repositories cloned        | N/A                   |
| **1**     | General Error           | Unexpected operational failure | Check logs            |
| **2**     | Configuration Error     | Invalid or missing settings    | Verify inputs         |
| **3**     | Discovery Error         | Failed to discover projects    | Check network and API |
| **4**     | Gerrit Connection Error | SSH auth or connection failed  | Verify SSH keys       |
| **5**     | Network Error           | Network connectivity issues    | Check connection      |
| **6**     | Repository Error        | Git operation failed           | Verify permissions    |
| **7**     | Clone Error             | Clone operations failed        | Check access          |
| **8**     | Validation Error        | Input validation failed        | Check parameters      |
| **9**     | Filesystem Error        | Filesystem access issues       | Check disk space      |
| **130**   | Interrupted             | Cancelled by user (Ctrl+C)     | N/A                   |

<!-- markdownlint-enable MD013 -->

### Discovery Method Improvements

When using `--discovery-method both`, the tool now:

- **Attempts both HTTP and SSH discovery** methods simultaneously
- **Creates a union of all projects** found by either method when both succeed
- **Prefers SSH metadata** for duplicate projects (same name) as SSH typically
  provides more complete information
- **Continues with working method** if one fails, logging a warning
- **Exits with code 3** if both methods fail (fatal discovery error)
- **Includes warnings in output** about any discovery method failures and
  discrepancies

This provides better reliability and completeness by ensuring all projects get
cloned, while maintaining visibility into discovery issues between the two methods.

## Development

### Requirements

- Python 3.11+ (tested on 3.11, 3.12, 3.13, 3.14)
- uv package manager (for development)
- Git (for clone operations)
- SSH client (for authentication)

### Setup

```bash
git clone https://github.com/lfreleng-actions/gerrit-clone-action.git
cd gerrit-clone-action
uv sync --dev
```

### Testing

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=gerrit_clone --cov-report=html --cov-report=term-missing

# Run integration tests (requires network)
uv run pytest tests/integration/ -v

# Run specific test categories
uv run pytest -m "not integration" -v  # Unit tests
uv run pytest tests/test_models.py::TestConfig -v  # Specific test class
```

### Linting

```bash
# Run pre-commit hooks
uv run pre-commit run --all-files

# Individual tools
uv run ruff check .
uv run ruff format .
uv run mypy src/
```

## License

This project uses the Apache License 2.0. See LICENSE for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with tests
4. Run linting and tests
5. Submit a pull request

## Support

- **GitHub Issues**: Report bugs and request features at
  [lfreleng-actions/gerrit-clone-action](https://github.com/lfreleng-actions/gerrit-clone-action/issues)
- **Documentation**: This README, IMPLEMENTATION.md, and inline help
  (`gerrit-clone --help`)
- **Examples**: Advanced usage patterns in repository examples/
- **Integration Tests**: Real-world server validation in tests/integration/
