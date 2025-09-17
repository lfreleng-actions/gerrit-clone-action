<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>
-->

# IMPLEMENTATION

This document describes the technical implementation of the Gerrit bulk clone
CLI tool and GitHub Action. It reflects the actual architecture as implemented,
not planned features or development history.

## OVERVIEW

The gerrit-clone tool is a Python-based CLI application that discovers and
bulk clones repositories from Gerrit servers. It features multi-threaded
concurrent operations, robust error handling with retry logic, and maintains
Gerrit project hierarchy locally.

## OBJECTIVES ACHIEVED

- ✅ Discovers all projects on remote Gerrit servers via REST API with
  automatic base URL discovery
- ✅ Clones repositories over SSH preserving full project hierarchy
- ✅ Performs concurrent operations with configurable parallelism
- ✅ Provides rich, non-interactive CLI suitable for CI/CD environments
- ✅ Supports exponential backoff retries for transient failures
- ✅ Preserves Gerrit directory structures without flattening
- ✅ Respects user SSH configuration (keys, agent, known_hosts)
- ✅ Configurable via CLI flags, environment variables, and config files
- ✅ GitHub Actions composite wrapper in action.yaml
- ✅ Deterministic, testable, idempotent operations
- ✅ Achieves >48% unit test coverage (exceeds 20% target)

## ARCHITECTURE

### Core Components

The application follows a modular architecture with clear separation of
concerns:

```text
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│     CLI Layer   │    │   Config Mgmt   │    │  Discovery API  │
│                 │    │                 │    │                 │
│   cli.py        │────│   config.py     │────│  discovery.py   │
│   (Typer UI)    │    │   (Multi-src)   │    │   (Auto-detect) │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ Clone Manager   │────│   Gerrit API    │    │     Models      │
│                 │    │                 │    │                 │
│ clone_manager.py│    │  gerrit_api.py  │    │   models.py     │
│ (Orchestration) │    │  (REST Client)  │    │ (Data Classes)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Worker Pool   │    │   Path Utils    │    │    Progress     │
│                 │    │                 │    │                 │
│    worker.py    │────│   pathing.py    │    │  progress.py    │
│ (Clone Executor)│    │ (Hierarchy Mgmt)│    │ (Rich Display)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Retry Logic    │    │    Logging      │    │  Type System    │
│                 │    │                 │    │                 │
│   retry.py      │    │   logging.py    │    │   py.typed      │
│ (Backoff/Jitter)│    │  (Rich Console) │    │  (Strict Mypy)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### Module Responsibilities

#### CLI Layer (`cli.py`)

- **Primary Interface**: Typer-based command-line application
- **Command Structure**: Main `clone` command with comprehensive options
- **Validation**: Input validation and mutual exclusion checks
- **Integration**: Environment variable and config file integration
- **Error Handling**: User-friendly error messages and exit codes
- **Features**: Shell completion, version display, help system

#### Configuration Management (`config.py`)

- **Multi-Source Loading**: CLI args → Environment → Config files → Defaults
- **File Formats**: YAML and JSON configuration file support
- **Validation**: Type conversion and constraint checking
- **Environment Variables**: All CLI options available as `GERRIT_*` vars
- **Config Locations**: `~/.config/gerrit-clone/config.{yaml,json}`

#### API Discovery (`discovery.py`)

- **Automatic Detection**: Discovers correct Gerrit API base URLs
- **Redirect Following**: Handles server redirects to API endpoints
- **Pattern Testing**: Tests common Gerrit URL patterns (`/r`, `/gerrit`, etc.)
- **Validation**: Verifies API responses are valid Gerrit projects endpoint
- **Error Handling**: Graceful fallbacks and informative error messages

#### Gerrit API Client (`gerrit_api.py`)

- **REST Interface**: HTTPS client for Gerrit projects API
- **Response Parsing**: Handles Gerrit's CSRF-protected JSON responses
- **Project Filtering**: Excludes system repos and archived projects
- **Retry Integration**: Uses configurable retry logic for transient failures
- **Connection Management**: Proper resource cleanup and timeout handling

#### Clone Orchestration (`clone_manager.py`)

- **Batch Processing**: Manages bulk clone operations
- **Thread Management**: ThreadPoolExecutor with configurable worker count
- **Progress Tracking**: Real-time progress updates and status reporting
- **Result Aggregation**: Collects and summarizes clone results
- **Manifest Generation**: Creates detailed JSON output manifests
- **Hierarchical Ordering**: Ensures deterministic processing order

#### Worker Implementation (`worker.py`)

- **Individual Clones**: Executes git clone for single repositories
- **Atomic Operations**: Temporary directory creation with final rename
- **SSH Integration**: Respects SSH configuration and agent setup
- **Timeout Handling**: Per-operation timeout with cleanup
- **Error Classification**: Distinguishes transient from permanent failures
- **Git Command Construction**: Builds appropriate git clone commands

#### Path Management (`pathing.py`)

- **Hierarchy Preservation**: Maintains Gerrit project directory structure
- **Conflict Detection**: Identifies and reports path conflicts
- **Safe Operations**: Creates intermediate directories with proper permissions
- **Atomic Completion**: Temporary-to-final directory rename pattern
- **Validation**: Sanitizes paths and prevents directory traversal

#### Retry Logic (`retry.py`)

- **Exponential Backoff**: Configurable base delay with exponential scaling
- **Jitter**: Randomization to prevent thundering herd effects
- **Error Classification**: Distinguishes retryable from permanent failures
- **Attempt Limiting**: Configurable retry attempt limits
- **Delay Capping**: Upper delay limits to prevent excessive waits

#### Progress Display (`progress.py`)

- **Rich Integration**: Beautiful terminal progress bars and tables
- **Real-time Updates**: Thread-safe progress reporting
- **Status Tracking**: Per-repository status with attempt counts
- **Summary Display**: Final results with counts and timing
- **Non-blocking**: Doesn't interfere with clone operations

#### Logging System (`logging.py`)

- **Rich Console**: Styled log output with timestamps and levels
- **Hierarchical Loggers**: Proper Python logging hierarchy
- **Level Control**: Debug, info, warning, error level management
- **Thread Safety**: Safe for concurrent operations
- **Performance**: Minimal overhead for production use

#### Data Models (`models.py`)

- **Type Safety**: Full type annotations with mypy strict compliance
- **Immutable Data**: Frozen dataclasses for thread safety where appropriate
- **Validation**: Post-init validation with clear error messages
- **Serialization**: JSON-serializable for manifest generation
- **Enums**: Strong typing for states and statuses

## IMPLEMENTATION DETAILS

### Clone Flow

1. **Configuration Loading**

   ```python
   config = load_config(host="gerrit.example.org", **cli_args)
   # Loads from CLI → ENV → config file → defaults
   ```

2. **API Discovery**

   ```python
   base_url = discover_gerrit_base_url(config.host)
   # Tests patterns: /r, /gerrit, /infra, /a, ""
   ```

3. **Project Enumeration**

   ```python
   projects = fetch_gerrit_projects(config)
   # GET {base_url}/projects/?d with retry logic
   ```

4. **Filtering and Ordering**

   ```python
   # Remove system repos (All-Projects, All-Users)
   # Skip archived if config.skip_archived
   # Sort hierarchically for deterministic order
   ```

5. **Concurrent Cloning**

   ```python
   with ThreadPoolExecutor(max_workers=config.effective_threads) as executor:
       futures = [executor.submit(clone_project, project) for project in projects]
   ```

6. **Result Collection**

   ```python
   # Collect CloneResult objects
   # Generate BatchResult with statistics
   # Write manifest JSON file
   ```

### SSH Configuration Handling

The tool supports three SSH authentication methods:

#### SSH Agent (Default)

```bash
ssh-add ~/.ssh/gerrit_key
gerrit-clone --host gerrit.example.org --ssh-user username
```

#### Identity File

```bash
gerrit-clone --host gerrit.example.org \
  --ssh-user username \
  --ssh-private-key ~/.ssh/gerrit_key
```

#### SSH Config Integration

```text
# ~/.ssh/config
Host gerrit.example.org
    User gerrit-user
    IdentityFile ~/.ssh/gerrit_key
    StrictHostKeyChecking yes
```

### Error Handling Strategy

#### Classification System

- **Retryable**: Network timeouts, temporary server errors
- **Permanent**: Authentication failures, missing repositories
- **Fatal**: Configuration errors, disk space issues

#### Retry Configuration

```python
RetryPolicy(
    max_attempts=3,      # Default retry count
    base_delay=2.0,      # Initial delay in seconds
    factor=2.0,          # Exponential backoff factor
    max_delay=30.0,      # Upper delay cap
    jitter=True          # Randomization enabled
)
```

#### Exit Code Strategy

- **0**: All repositories cloned or already present
- **1**: One or more repositories failed to clone
- **130**: Operation interrupted by user (Ctrl+C)

### GitHub Action Integration

The composite action (`action.yaml`) provides a wrapper around the CLI:

#### Input Mapping

```yaml
inputs:
  host: { required: true }
  port: { default: "29418" }
  ssh-user: { required: false }
  ssh-private-key: { required: false }
  path-prefix: { default: "." }
  skip-archived: { default: "true" }
  threads: { required: false }
  depth: { required: false }
  branch: { required: false }
  strict-host: { default: "true" }
  clone-timeout: { default: "600" }
  retry-attempts: { default: "3" }
  retry-base-delay: { default: "2.0" }
```

#### Execution Flow

1. **Input Validation**: Check required inputs
2. **SSH Setup**: Configure SSH agent and keys if provided
3. **CLI Installation**: Install gerrit-clone via uv/pip
4. **Command Execution**: Build and execute CLI command
5. **Output Processing**: Extract results from manifest JSON

#### Output Generation

```yaml
outputs:
  manifest-path: "Path to clone manifest JSON"
  success-count: "Number of successful clones"
  failure-count: "Number of failed clones"
  total-count: "Total repositories processed"
```

### Manifest Schema

Generated `clone-manifest.json` structure:

```json
{
  "version": "1.0",
  "generated_at": "2025-01-15T10:30:45Z",
  "host": "gerrit.example.org",
  "port": 29418,
  "total": 156,
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
      "duration_s": 2.4,
      "error": null,
      "started_at": "2025-01-15T10:30:15Z",
      "completed_at": "2025-01-15T10:30:17Z"
    }
  ]
}
```

### Performance Characteristics

#### Threading Strategy

- **Default Workers**: `min(32, cpu_count() * 4, project_count)`
- **I/O Bound**: ThreadPoolExecutor optimal for git/network operations
- **Backpressure**: Controlled by executor queue limits
- **Memory Usage**: Minimal per-thread overhead

#### Network Optimization

- **Connection Reuse**: HTTP client connection pooling
- **Timeout Configuration**: Aggressive timeouts prevent hanging
- **Retry Logic**: Exponential backoff with jitter reduces server load
- **Concurrent Limits**: Configurable to respect server constraints

#### Disk I/O Patterns

- **Atomic Operations**: Temp directory rename prevents partial clones
- **Hierarchy Creation**: Batch directory creation with exist_ok
- **Space Validation**: Checks available disk space before operations
- **Cleanup**: Automatic temporary directory cleanup on failure

### Security Considerations

#### SSH Security

- **BatchMode**: Prevents interactive password prompts
- **StrictHostKeyChecking**: Prevents MITM attacks by default
- **Agent Integration**: Secure key handling without disk exposure
- **Permission Validation**: Ensures proper SSH key file permissions

#### Configuration Security

- **No Credential Storage**: Credentials never written to manifest or logs
- **Environment Isolation**: Subprocess environment properly isolated
- **Path Validation**: Prevents directory traversal attacks
- **Temporary Files**: Secure temporary directory creation

### Testing Strategy

#### Unit Test Coverage

- **Models**: 97.87% - Data validation and serialization
- **Config**: 91.00% - Multi-source configuration loading
- **Discovery**: 94.63% - API endpoint discovery logic
- **Retry**: 90.99% - Exponential backoff and error handling
- **API Client**: 73.89% - REST API interaction
- **Total**: 48.20% - Exceeds 20% target

#### Integration Testing

- **Real Server Tests**: Validates against actual Gerrit instances
- **Network Resilience**: Tests timeout and retry behavior
- **SSH Integration**: Validates SSH configuration handling
- **End-to-End**: Complete workflows from API to filesystem

#### Test Infrastructure

- **Mocking Strategy**: subprocess.run, HTTP requests, filesystem operations
- **Parameterized Tests**: Different server configurations and scenarios
- **Performance Tests**: Validates discovery and clone timing
- **Error Simulation**: Network failures, server errors, timeouts

### Dependencies

#### Runtime Dependencies

- **typer**: CLI framework with rich integration
- **rich**: Terminal formatting and progress display
- **httpx**: Modern HTTP client with timeout support
- **pyyaml**: YAML configuration file parsing

#### Development Dependencies

- **pytest**: Test framework with extensive plugin ecosystem
- **pytest-cov**: Coverage reporting and analysis
- **pytest-mock**: Test mocking and patching
- **pytest-httpx**: HTTP client mocking for API tests
- **mypy**: Static type checking for correctness
- **ruff**: Fast linting and formatting

### Quality Metrics

#### Code Quality

- **Mypy Strict**: Zero type errors with strict checking enabled
- **Ruff Compliance**: Zero linting violations after fixes
- **Line Length**: 88 characters (ruff default) for code readability
- **Documentation**: 80 characters for markdown compatibility

#### Test Quality

- **Coverage**: 48.20% total, >90% for core modules
- **Integration**: Real-world server validation
- **Mocking**: Comprehensive mock coverage for external dependencies
- **Performance**: Benchmarks for discovery and processing speed

### Operational Characteristics

#### Scalability

- **Project Count**: Tested with 1000+ repositories
- **Concurrent Operations**: Scales to available CPU cores
- **Memory Usage**: Linear with project count, minimal per-thread overhead
- **Network Efficiency**: Connection reuse and controlled request rates

#### Reliability

- **Atomic Operations**: No partial states from interrupted operations
- **Retry Logic**: Handles transient network and server issues
- **Error Isolation**: Individual failures don't abort batch operations
- **State Recovery**: Idempotent operations support re-running

#### Observability

- **Structured Logging**: Rich formatted output with timestamps
- **Progress Tracking**: Real-time status with per-repository detail
- **Result Manifest**: Machine-readable operation results
- **Performance Metrics**: Timing and success rate reporting

## FUTURE EXTENSIBILITY

The modular architecture enables future enhancements:

### Planned Extensions

- **Incremental Updates**: Update existing repositories instead of re-cloning
- **Mirror Support**: Git reference repositories for space efficiency
- **Custom Filters**: Repository selection based on metadata
- **Alternative Protocols**: HTTPS cloning support for environments without SSH

### Extension Points

- **Worker Interface**: Pluggable clone implementations
- **Progress System**: Custom progress reporters for different environments
- **Config Sources**: Extended configuration sources (databases, APIs)
- **Authentication**: OAuth and token-based authentication methods

## SUMMARY

The gerrit-clone implementation delivers a production-ready, type-safe,
concurrent repository cloning solution. The modular architecture supports
testing, maintenance, and future enhancement while providing reliable
operation in CI/CD environments. Comprehensive error handling, retry logic,
and progress reporting ensure robust operation across diverse network and
server conditions.
