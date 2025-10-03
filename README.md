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
  pools (up to 32 workers)
- **Hierarchy Preservation**: Maintains complete Gerrit project folder
  structure without flattening
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

### Basic Examples

Clone all active repositories from a Gerrit server:

```bash
gerrit-clone --host gerrit.example.org
```

Clone to a specific directory with custom thread count:

```bash
gerrit-clone --host gerrit.example.org \
  --path-prefix ./repositories \
  --threads 8
```

Clone with shallow depth and specific branch:

```bash
gerrit-clone --host gerrit.example.org \
  --depth 10 \
  --branch main \
  --threads 16
```

Include archived repositories and use custom SSH key:

```bash
gerrit-clone --host gerrit.example.org \
  --include-archived \
  --ssh-user myuser \
  --ssh-private-key ~/.ssh/gerrit_rsa
```

### Command-Line Options

```text
Usage: gerrit-clone [OPTIONS]

Options:
  -h, --host TEXT                 Gerrit server hostname [required]
  -p, --port INTEGER              Gerrit SSH port [default: 29418]
  --base-url TEXT                 Base URL for Gerrit API
  -u, --ssh-user TEXT             SSH username for clone operations
  -i, --ssh-private-key PATH      SSH private key file for authentication
  --path-prefix PATH              Base directory for clone hierarchy [default: .]
  --skip-archived / --include-archived
                                  Skip archived and inactive repositories
                                  [default: skip-archived]
  --include-project TEXT          Restrict cloning to specific project(s)
  --ssh-debug                     Enable verbose SSH (-vvv) for troubleshooting
  --allow-nested-git / --no-allow-nested-git
                                  Allow nested git working trees when cloning
                                  both parent and child repositories [default: allow-nested-git]
  --nested-protection / --no-nested-protection
                                  Auto-add nested child repo paths to parent
                                  .git/info/exclude [default: nested-protection]
  --move-conflicting              Move conflicting files/directories in parent
                                  repos to [NAME].parent to allow nested cloning
  -t, --threads INTEGER           Number of concurrent clone threads
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
  --manifest-filename TEXT        Output manifest filename [default: clone-manifest.json]
  -c, --config-file PATH          Configuration file path (YAML or JSON)
  --exit-on-error                 Exit when first error occurs
  --log-file PATH                 Custom log file path
  --disable-log-file              Disable creation of log file
  --log-level TEXT                File logging level [default: DEBUG]
  -v, --verbose                   Enable verbose/debug output
  -q, --quiet                     Suppress all output except errors
  --version                       Show version information
  --help                          Show this message and exit
```

### Environment Variables

You can configure all CLI options through environment variables with `GERRIT_` prefix:

```bash
export GERRIT_HOST=gerrit.example.org
export GERRIT_PORT=29418
export GERRIT_SSH_USER=myuser
export GERRIT_SSH_PRIVATE_KEY=~/.ssh/gerrit_key
export GERRIT_PATH_PREFIX=/workspace/repos
export GERRIT_SKIP_ARCHIVED=1
export GERRIT_THREADS=16
export GERRIT_CLONE_DEPTH=5
export GERRIT_BRANCH=main
export GERRIT_STRICT_HOST=1
export GERRIT_CLONE_TIMEOUT=300
export GERRIT_RETRY_ATTEMPTS=5

gerrit-clone  # Uses environment variables
```

### Configuration Files

Create `~/.config/gerrit-clone/config.yaml`:

```yaml
host: gerrit.example.org
port: 29418
ssh_user: myuser
ssh_identity_file: ~/.ssh/gerrit_key
path_prefix: /workspace/repos
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
  "path_prefix": "/workspace/repos",
  "skip_archived": true,
  "threads": 8
}
```

Configuration precedence: CLI arguments > Environment variables > Config file > Defaults

## Nested Repository Support

Gerrit Clone includes intelligent support for nested repositories (projects with
hierarchical names like `parent/child`):

### Automatic Detection

- **Dependency Ordering**: Parent repositories are automatically cloned before
  their children
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
- Result: Both repositories cloned with complete history preserved

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
          path-prefix: repositories
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
          path-prefix: workspace
          skip-archived: true
          threads: 12
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
          path: ${{ steps.clone.outputs.manifest-path }}
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
          path-prefix: repos
          quiet: true
        env:
          # Use GitHub token or other auth for HTTPS
          GIT_ASKPASS: echo
          GIT_USERNAME: ${{ secrets.GERRIT_USERNAME }}
          GIT_PASSWORD: ${{ secrets.GERRIT_TOKEN }}
```

### Nested Repositories with Conflict Resolution

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
          path-prefix: complete-clone
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

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `host` | Yes | | Gerrit server hostname |
| `port` | No | `29418` | Gerrit SSH port |
| `base-url` | No | | Base URL for Gerrit API (defaults to <https://HOST>) |
| `ssh-user` | No | | SSH username for clone operations |
| `ssh-private-key` | No | | SSH private key content for authentication |
| `path-prefix` | No | `.` | Base directory for clone hierarchy |
| `skip-archived` | No | `true` | Skip archived and inactive repositories |
| `include-project` | No | | Restrict cloning to specific project(s) (comma-separated) |
| `ssh-debug` | No | `false` | Enable verbose SSH (-vvv) for troubleshooting |
| `allow-nested-git` | No | `true` | Allow nested git working trees |
| `nested-protection` | No | `true` | Auto-add nested child repo paths to parent .git/info/exclude |
| `move-conflicting` | No | `false` | Move conflicting files/directories in parent repos to [NAME].parent |
| `exit-on-error` | No | `false` | Exit when first error occurs |
| `threads` | No | auto | Number of concurrent clone threads |
| `depth` | No | | Create shallow clone with given depth |
| `branch` | No | | Clone specific branch instead of default |
| `use-https` | No | `false` | Use HTTPS for cloning instead of SSH |
| `keep-remote-protocol` | No | `false` | Keep original clone protocol for remote |
| `strict-host` | No | `true` | SSH strict host key checking |
| `clone-timeout` | No | `600` | Timeout per clone operation in seconds |
| `retry-attempts` | No | `3` | Max retry attempts per repository |
| `retry-base-delay` | No | `2.0` | Base delay for retry backoff in seconds |
| `retry-factor` | No | `2.0` | Exponential backoff factor for retries |
| `retry-max-delay` | No | `30.0` | Max retry delay in seconds |
| `manifest-filename` | No | `clone-manifest.json` | Output manifest filename |
| `config-file` | No | | Configuration file path (YAML or JSON) |
| `verbose` | No | `false` | Enable verbose/debug output |
| `quiet` | No | `false` | Suppress all output except errors |
| `log-file` | No | | Custom log file path |
| `disable-log-file` | No | `false` | Disable creation of log file |
| `log-level` | No | `DEBUG` | File logging level |

<!-- markdownlint-enable MD013 -->

### Action Outputs

| Output | Description |
|--------|-------------|
| `manifest-path` | Path to the generated clone manifest file |
| `success-count` | Number of cloned repositories |
| `failure-count` | Number of failed clone attempts |
| `total-count` | Total number of repositories processed |

## SSH Configuration

The tool provides comprehensive SSH authentication support with automatic
configuration detection:

### SSH Authentication Options

The following SSH authentication options are available across all interfaces:

<!-- markdownlint-disable MD013 -->
| Option | CLI | Environment | Action | Description |
|--------|-----|-------------|--------|-------------|
| SSH User | `-u` | `GERRIT_SSH_USER` | `ssh-user` | SSH username |
| SSH Key | `-i` (file) | `GERRIT_SSH_PRIVATE_KEY` | `ssh-private-key` (content) | Private key |
| Host Check | `--strict-host` | `GERRIT_STRICT_HOST` | `strict-host` | Key check |
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
    "path_prefix": "/workspace/repos"
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
- Use clean target directory: `--path-prefix ./clean-workspace`
- Check disk space and write permissions
- Remove conflicting directories: `rm -rf conflicting-path`

#### API discovery failures

- Manually specify base URL: `--base-url https://host/gerrit`
- Verify Gerrit server is accessible via HTTPS
- Check for corporate proxy or firewall restrictions

### Exit Codes

- `0`: Success (all repositories cloned)
- `1`: Failure (one or more repositories failed to clone)
- `130`: Interrupted by user (Ctrl+C)

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
