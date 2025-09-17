# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Clone worker for individual repository operations."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Cross-platform file locking imports
if sys.platform == "win32":
    pass
else:
    pass

from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project
from gerrit_clone.pathing import check_path_conflicts, get_project_path

logger = get_logger(__name__)


class CloneError(Exception):
    """Base exception for clone operations."""


class CloneTimeoutError(CloneError):
    """Raised when clone operation times out."""


@contextmanager
def _file_lock(
    lock_file_path: Path, timeout: float = 30.0
) -> Generator[None, None, None]:
    """Cross-platform file locking using atomic file creation.

    This uses atomic file creation as the locking mechanism, which is more
    reliable across platforms than fcntl/msvcrt locking.

    Args:
        lock_file_path: Path to the lock file
        timeout: Maximum time to wait for lock acquisition

    Yields:
        None when lock is acquired

    Raises:
        OSError: If lock cannot be acquired within timeout
    """
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    start_time = time.time()

    try:
        # Try to acquire lock using atomic file creation
        while True:
            try:
                # Try to create lock file exclusively (atomic operation)
                with open(lock_file_path, "x") as lock_file:
                    # Write process info for debugging
                    lock_file.write(f"pid:{os.getpid()}\ntime:{time.time()}\n")
                    lock_file.flush()
                acquired = True
                break  # Lock acquired successfully
            except FileExistsError:
                # Lock file already exists, check if it's stale
                if time.time() - start_time > timeout:
                    # Check if lock file might be stale
                    try:
                        # If lock file is older than timeout, it might be stale
                        if lock_file_path.exists():
                            stat = lock_file_path.stat()
                            if time.time() - stat.st_mtime > timeout:
                                # Try to remove stale lock
                                lock_file_path.unlink()
                                continue  # Try again
                    except OSError:
                        pass

                    raise OSError(
                        f"Could not acquire lock within {timeout}s: {lock_file_path}"
                    )

                # Wait briefly before retry
                time.sleep(0.05)  # 50ms wait

        yield

    finally:
        # Clean up lock file if we acquired it
        if acquired and lock_file_path.exists():
            try:
                lock_file_path.unlink()
            except OSError:
                pass  # Cleanup can fail, don't break the operation


class CloneWorker:
    """Worker for cloning individual repositories."""

    def __init__(self, config: Config) -> None:
        """Initialize clone worker.

        Args:
            config: Configuration for clone operations
        """
        self.config = config

    def clone_project(self, project: Project) -> CloneResult:
        """Clone a single project repository.

        Args:
            project: Project to clone

        Returns:
            CloneResult with operation details
        """
        logger.info(f"🔄 Processing {project.name}")
        target_path = get_project_path(project.name, self.config.path_prefix)
        started_at = datetime.now(UTC)

        # Initialize result object
        result = CloneResult(
            project=project,
            status=CloneStatus.PENDING,
            path=target_path,
            started_at=started_at,
        )

        try:
            # Create lock file path for this project
            lock_file = target_path.parent / f".{target_path.name}.clone_lock"

            # Use file locking to prevent race conditions between threads
            logger.debug(f"Attempting to acquire clone lock for {project.name}")

            try:
                with _file_lock(lock_file):
                    logger.info(
                        f"📁 Validating path for {project.name} (lock acquired)"
                    )
                    logger.debug(f"Checking path conflicts for: {target_path}")
                    logger.debug(f"Target path exists: {target_path.exists()}")
                    if target_path.exists():
                        logger.debug(
                            f"Target path is directory: {target_path.is_dir()}"
                        )
                        if target_path.is_dir():
                            contents = (
                                list(target_path.iterdir())
                                if target_path.is_dir()
                                else []
                            )
                            logger.debug(
                                f"Directory contents: {[p.name for p in contents]}"
                            )

                    # Check for path conflicts (now atomic with clone operation)
                    conflict = check_path_conflicts(target_path)
                    logger.debug(
                        f"Path conflict check result for {project.name}: {conflict}"
                    )
                    if conflict is not None:
                        logger.debug(
                            f"Conflict detected for {project.name}: {conflict}"
                        )
                        if conflict == "already_cloned":
                            logger.debug(
                                f"Repository {project.name} already cloned, marking as ALREADY_EXISTS"
                            )
                            result.status = CloneStatus.ALREADY_EXISTS
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.info(
                                f"✓ Repository {project.name} already exists - skipped"
                            )
                            return result
                        else:
                            logger.debug(
                                f"Non-cloned conflict for {project.name}, marking as FAILED and returning early"
                            )
                            result.status = CloneStatus.FAILED
                            result.error_message = f"Path conflict: {conflict}"
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.error(
                                f"Path conflict for [project]{project.name}[/project]: {conflict}"
                            )
                            logger.debug(
                                f"Early return for {project.name} due to path conflict"
                            )
                            return result

                    # No path conflict detected, proceeding with clone
                    logger.debug(
                        f"No path conflict for {project.name}, proceeding with clone"
                    )

                    # Update status to cloning
                    result.status = CloneStatus.CLONING

                    # Perform clone with adaptive retry based on error patterns
                    # (still within the lock to prevent race conditions)
                    logger.debug(f"Starting clone execution for {project.name}")
                    success = self._execute_adaptive_clone(project, target_path, result)
                    logger.debug(
                        f"Clone execution completed for {project.name}, success: {success}"
                    )

                    # Handle success/failure within the lock
                    if success:
                        result.status = CloneStatus.SUCCESS
                    else:
                        result.status = CloneStatus.FAILED
                        if not result.error_message:
                            result.error_message = "Clone failed for unknown reason"

            except OSError as lock_error:
                # Lock acquisition failed - likely another thread is already cloning this project
                result.status = CloneStatus.FAILED
                result.error_message = f"Could not acquire clone lock: {lock_error}"
                result.completed_at = datetime.now(UTC)
                result.duration_seconds = (
                    result.completed_at - started_at
                ).total_seconds()
                logger.warning(
                    f"Failed to acquire lock for [project]{project.name}[/project]: {lock_error}"
                )
                return result

        except Exception as e:
            result.status = CloneStatus.FAILED
            result.error_message = str(e)
            logger.error(f"Failed to clone [project]{project.name}[/project]: {e}")

        finally:
            result.completed_at = datetime.now(UTC)
            result.duration_seconds = (result.completed_at - started_at).total_seconds()

        return result

    def _execute_adaptive_clone(
        self, project: Project, target_path: Path, result: CloneResult
    ) -> bool:
        """Execute clone with adaptive retry based on filesystem conditions.

        Args:
            project: Project to clone
            target_path: Target path for clone
            result: Result object to update

        Returns:
            True if clone succeeded, False otherwise
        """
        max_attempts = self.config.retry_policy.max_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                success = self._perform_clone(project, target_path, result)
                if success:
                    return True

                # Clone failed - determine if we should retry
                error_msg = result.error_message or ""

                # Don't retry non-retryable errors
                if not self._is_filesystem_error_retryable(error_msg):
                    logger.info(
                        f"Non-retryable error for {project.name}: {error_msg[:100]}..."
                    )
                    return False

                # Calculate adaptive delay based on error type
                delay = self._calculate_adaptive_delay(attempt, error_msg)

                if attempt < max_attempts:
                    logger.warning(
                        f"Retry clone {project.name} (attempt {attempt + 1}/{max_attempts}) after {delay:.2f}s: {error_msg[:100]}..."
                    )
                    time.sleep(delay)

            except Exception as e:
                result.error_message = str(e)
                logger.error(f"Unexpected error cloning {project.name}: {e}")
                return False

        return False

    def _is_filesystem_error_retryable(self, error_msg: str) -> bool:
        """Determine if a filesystem error should be retried.

        Args:
            error_msg: Error message to analyze

        Returns:
            True if error should be retried
        """
        error_lower = error_msg.lower()

        # File not found errors are generally not retryable
        if "no such file or directory" in error_lower:
            # Exception: temporary files during operations might be retryable
            if any(pattern in error_lower for pattern in ["tmp_", "temp", ".tmp"]):
                return True
            return False

        # Config file locking errors are retryable only if it's actual lock contention
        if "could not lock config file" in error_lower:
            # If the config file doesn't exist, it's not a lock issue
            if "no such file or directory" in error_lower:
                return False
            return True

        # .git directory access issues are retryable if not missing files
        if "could not lock" in error_lower and ".git" in error_lower:
            if "no such file or directory" in error_lower:
                return False
            return True

        # Filesystem I/O errors are generally retryable
        if any(
            pattern in error_lower
            for pattern in [
                "device or resource busy",
                "resource temporarily unavailable",
                "temporary failure",
                "no space left on device",
                "disk full",
                "input/output error",
                "broken pipe",
            ]
        ):
            return True

        # Post-transfer "could not open" errors - only retryable if not missing files
        if "fatal: could not open" in error_lower:
            if "no such file or directory" in error_lower:
                return False
            # If it's after pack transfer, could be transient
            if "total" in error_lower or "delta" in error_lower:
                return True

        # Repository not found is not retryable
        if "repository not found" in error_lower or "not found" in error_lower:
            return False

        # Permission errors are not retryable
        if "permission denied" in error_lower or "access denied" in error_lower:
            return False

        # Authentication failures are not retryable
        if (
            "authentication failed" in error_lower
            or "host key verification failed" in error_lower
        ):
            return False

        # Git setup errors are not retryable
        if "fatal: --stdin requires a git repository" in error_lower:
            return False

        # Default to retryable for unknown filesystem errors
        return True

    def _calculate_adaptive_delay(self, attempt: int, error_msg: str) -> float:
        """Calculate adaptive delay based on error type and attempt.

        Args:
            attempt: Current attempt number (1-based)
            error_msg: Error message to analyze

        Returns:
            Delay in seconds
        """
        error_lower = error_msg.lower()

        # Config file locking errors get very short delays - these are transient
        if "could not lock config file" in error_lower:
            base_delay = 0.2
            max_delay = 1.5
        # Filesystem I/O errors after pack transfer - short delays, likely transient
        elif "could not open" in error_lower and (
            "total" in error_lower or "delta" in error_lower
        ):
            base_delay = 0.5
            max_delay = 2.0
        # Generic filesystem errors - moderate delays
        elif any(
            pattern in error_lower
            for pattern in ["could not open", "device busy", "resource busy"]
        ):
            base_delay = 1.0
            max_delay = 4.0
        # Disk space errors get longer delays
        elif "no space left" in error_lower or "disk full" in error_lower:
            base_delay = 5.0
            max_delay = 15.0
        # Network errors get standard delays
        elif any(
            pattern in error_lower
            for pattern in [
                "timeout",
                "connection",
                "network",
                "early eof",
                "remote end hung up",
            ]
        ):
            base_delay = 2.0
            max_delay = 10.0
        # SSH/authentication errors - longer delays to avoid hammering
        elif any(
            pattern in error_lower
            for pattern in ["ssh", "authentication", "permission"]
        ):
            base_delay = 3.0
            max_delay = 12.0
        else:
            # Default delays for unknown errors
            base_delay = 1.0
            max_delay = 8.0

        # Exponential backoff with jitter
        delay = base_delay * (1.4 ** (attempt - 1))
        delay = min(delay, max_delay)

        # Add random jitter to prevent thundering herd (proportional to delay)
        import random

        jitter_factor = 0.2  # 20% jitter
        jitter = random.uniform(-jitter_factor * delay, jitter_factor * delay)
        return max(0.1, delay + jitter)  # Ensure minimum 100ms delay

    def _perform_clone(
        self, project: Project, target_path: Path, result: CloneResult
    ) -> bool:
        """Perform the actual clone operation with simple direct approach.

        Args:
            project: Project to clone
            target_path: Target path for clone
            result: Result object to update with attempt info

        Returns:
            True if clone succeeded, False otherwise

        Raises:
            CloneError: If clone fails with retryable error
            CloneTimeoutError: If clone times out
        """
        # Build clone command - clone directly to final path, let Git handle atomicity
        cmd = self._build_clone_command(project, target_path)
        env = self._build_clone_environment()

        result.attempts += 1
        logger.info(
            f"⬇️ Cloning {project.name} (attempt {result.attempts}/{self.config.retry_policy.max_attempts})"
        )
        logger.debug(
            f"Cloning [project]{project.name}[/project] (attempt {result.attempts})"
        )
        logger.debug(f"Clone command: {' '.join(cmd)}")

        try:
            logger.info(f"🔧 Executing git clone for {project.name}")
            logger.debug(f"Starting clone subprocess for {project.name}")
            start_time = datetime.now(UTC)

            # Execute git clone directly to target path - Git handles its own atomicity
            process_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.clone_timeout,
                env=env,
                cwd=self.config.path_prefix,
                check=False,
            )

            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            logger.debug(
                f"Clone subprocess completed for {project.name} in {duration:.1f}s"
            )

            if process_result.returncode == 0:
                # Set SSH remote if requested and we cloned with HTTPS
                if self.config.use_https and not self.config.keep_remote_protocol:
                    self._set_ssh_remote(project, target_path, env)

                logger.info(f"✅ Successfully cloned {project.name}")
                logger.debug(f"Successfully cloned [project]{project.name}[/project]")
                return True
            else:
                # Clone failed - analyze error
                error_msg = self._analyze_clone_error(process_result, project.name)
                result.error_message = error_msg

                # Determine if error is retryable
                if self._is_retryable_clone_error(process_result):
                    raise CloneError(error_msg)  # Will trigger retry
                else:
                    logger.error(
                        f"Non-retryable clone error for [project]{project.name}[/project]: {error_msg}"
                    )
                    return False

        except subprocess.TimeoutExpired:
            error_msg = f"Clone timeout after {self.config.clone_timeout}s"
            result.error_message = error_msg
            logger.warning(
                f"Clone timed out for {project.name} after {self.config.clone_timeout}s"
            )
            raise CloneTimeoutError(error_msg)

        except Exception as e:
            error_msg = f"Unexpected clone error: {e}"
            result.error_message = error_msg
            logger.error(f"Unexpected subprocess error for {project.name}: {e}")
            raise CloneError(error_msg)

    def _build_clone_command(self, project: Project, target_path: Path) -> list[str]:
        """Build git clone command for project.

        Args:
            project: Project to clone
            target_path: Target clone path

        Returns:
            Git clone command as list of strings
        """
        cmd = ["git", "clone"]

        # Add options to reduce filesystem contention and config access
        cmd.extend(
            [
                "--no-hardlinks",  # Prevent hardlink creation that can cause locks
                "--quiet",  # Reduce output and potential I/O contention
            ]
        )

        # Add depth option for shallow clone
        if self.config.depth is not None:
            cmd.extend(["--depth", str(self.config.depth)])

        # Add branch option
        if self.config.branch is not None:
            cmd.extend(["--branch", self.config.branch])

        # Build clone URL (HTTPS or SSH)
        if self.config.use_https:
            clone_url = self._build_https_url(project)
        else:
            clone_url = self._build_ssh_url(project)
        cmd.append(clone_url)

        # Target path (will be updated to temp path)
        cmd.append(str(target_path))

        return cmd

    def _build_ssh_url(self, project: Project) -> str:
        """Build SSH URL for project.

        Args:
            project: Project to clone

        Returns:
            SSH clone URL
        """
        user_prefix = f"{self.config.ssh_user}@" if self.config.ssh_user else ""
        return (
            f"ssh://{user_prefix}{self.config.host}:{self.config.port}/{project.name}"
        )

    def _build_https_url(self, project: Project) -> str:
        """Build HTTPS URL for project.

        Args:
            project: Project to clone

        Returns:
            HTTPS clone URL
        """
        return f"{self.config.base_url}/{project.name}"

    def _set_ssh_remote(
        self, project: Project, repo_path: Path, env: dict[str, str]
    ) -> None:
        """Set the remote URL to SSH after HTTPS clone with isolated environment.

        Args:
            project: Project that was cloned
            repo_path: Path to the cloned repository
            env: Isolated git environment to use
        """
        import random
        import time

        ssh_url = self._build_ssh_url(project)
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", ssh_url],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,  # Use isolated environment
                )
                logger.debug(
                    f"Set SSH remote for [project]{project.name}[/project]: {ssh_url}"
                )
                return
            except subprocess.SubprocessError as e:
                error_msg = str(e)
                # Check for config lock errors that warrant retry
                if (
                    "could not lock config file" in error_msg.lower()
                    or "no such file or directory" in error_msg.lower()
                    or (
                        "could not open" in error_msg.lower()
                        and ".git/config" in error_msg.lower()
                    )
                ):
                    if attempt < max_attempts:
                        # Small delay with jitter for config lock retry
                        delay = 0.2 + (random.uniform(0.1, 0.3) * attempt)
                        logger.debug(
                            f"Config lock detected for {project.name}, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_attempts})"
                        )
                        time.sleep(delay)
                        continue
                    else:
                        logger.warning(
                            f"Failed to set SSH remote for [project]{project.name}[/project] after {max_attempts} attempts: {e}"
                        )
                else:
                    logger.warning(
                        f"Failed to set SSH remote for [project]{project.name}[/project]: {e}"
                    )
                    return
            except Exception as e:
                logger.warning(
                    f"Unexpected error setting SSH remote for [project]{project.name}[/project]: {e}"
                )
                return

    def _create_isolated_git_config(self, config_dir: Path) -> None:
        """Create minimal git configuration in isolated directory.

        Args:
            config_dir: Directory to create git config in
        """
        try:
            # Create a minimal .gitconfig to prevent git from searching elsewhere
            gitconfig_path = config_dir / ".gitconfig"
            gitconfig_content = """[core]
    repositoryformatversion = 0
    filemode = true
    bare = false
    logallrefupdates = true
[gc]
    auto = 0
[receive]
    denyCurrentBranch = ignore
"""
            gitconfig_path.write_text(gitconfig_content)

            # Create empty known_hosts to prevent SSH prompts
            ssh_dir = config_dir / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "known_hosts").touch()

        except Exception as e:
            logger.debug(f"Could not create isolated git config: {e}")

    def _build_clone_environment(self) -> dict[str, str]:
        """Build environment variables for git clone.

        Returns:
            Environment dictionary
        """
        env = os.environ.copy()

        # Set GIT_SSH_COMMAND for strict host checking
        if self.config.git_ssh_command:
            env["GIT_SSH_COMMAND"] = self.config.git_ssh_command

        # Create thread-specific git configuration directory
        import tempfile
        import threading

        thread_id = threading.get_ident()
        git_config_dir = Path(tempfile.mkdtemp(prefix=f"git_config_{thread_id}_"))

        # Create minimal isolated git configuration
        self._create_isolated_git_config(git_config_dir)

        # Essential git environment isolation to prevent config file contention
        # Keep only the minimal set that prevents conflicts without breaking git
        env["GIT_CONFIG_GLOBAL"] = str(git_config_dir / ".gitconfig")
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["HOME"] = str(git_config_dir)  # Isolate home directory for git

        # Set aggressive timeouts to prevent hanging
        env["GIT_HTTP_LOW_SPEED_LIMIT"] = "1000"
        env["GIT_HTTP_LOW_SPEED_TIME"] = "30"

        # Disable git operations that could cause file locking
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_AUTO_GC"] = "0"

        return env

    def _analyze_clone_error(
        self, process_result: subprocess.CompletedProcess[str], project_name: str
    ) -> str:
        """Analyze clone error and return descriptive message.

        Args:
            process_result: Completed subprocess result
            project_name: Name of project that failed

        Returns:
            Descriptive error message
        """
        stderr = process_result.stderr.strip()
        stdout = process_result.stdout.strip()
        exit_code = process_result.returncode

        # Combine stderr and stdout for analysis
        error_output = f"{stderr}\n{stdout}".strip()

        # Common error patterns
        if "Permission denied" in error_output:
            return f"Permission denied - check SSH key and access to {project_name}"
        elif "Host key verification failed" in error_output:
            return (
                "Host key verification failed - add to known_hosts or run ssh-keyscan"
            )
        elif "Connection refused" in error_output:
            return f"Connection refused - check network connectivity to {self.config.host}:{self.config.port}"
        elif "could not resolve hostname" in error_output.lower():
            return f"Could not resolve hostname - check DNS settings for {self.config.host}"
        elif (
            "Repository not found" in error_output
            or "not found" in error_output.lower()
        ):
            return f"Repository not found: {project_name}"
        elif "could not lock config file" in error_output.lower():
            # Preserve actual error path instead of hardcoded placeholder
            lock_line = next(
                (
                    line
                    for line in error_output.split("\n")
                    if "could not lock config file" in line.lower()
                ),
                "",
            )
            if lock_line:
                return f"Git error: {lock_line.strip()}"
            else:
                return "Git error: could not lock config file (path not captured)"
        elif (
            "could not open" in error_output.lower()
            and "fatal:" in error_output.lower()
        ):
            # Preserve actual error details including paths
            fatal_line = next(
                (
                    line
                    for line in error_output.split("\n")
                    if "fatal: could not open" in line.lower()
                ),
                "",
            )
            if fatal_line:
                return f"Git error: {fatal_line.strip()}"
            else:
                return "Git error: fatal could not open (details not captured)"
        elif "timeout" in error_output.lower():
            return "Network timeout during clone"
        elif "early EOF" in error_output.lower():
            return "Connection terminated unexpectedly"
        elif "remote end hung up" in error_output.lower():
            return "Remote server disconnected"
        elif exit_code == 128:
            # Git error code 128 is general error
            if error_output:
                return f"Git error: {error_output[:200]}..."
            else:
                return f"Git error (exit code {exit_code})"
        elif error_output:
            # Try to find the most informative line (error/fatal/warning)
            important_line = None
            for line in error_output.split("\n"):
                if any(
                    keyword in line.lower()
                    for keyword in ["error:", "fatal:", "warning:", "failed"]
                ):
                    important_line = line.strip()
                    break

            if important_line:
                return f"Clone failed (exit code {exit_code}): {important_line}"
            else:
                return f"Clone failed (exit code {exit_code}): {error_output[:150]}..."
        else:
            return f"Clone failed with exit code {exit_code}"

    def _is_retryable_clone_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Check if a clone error is retryable.

        Args:
            process_result: Completed subprocess result

        Returns:
            True if error should be retried
        """
        stderr = process_result.stderr.strip()
        stdout = process_result.stdout.strip()
        error_output = f"{stderr}\n{stdout}".strip().lower()

        # Non-retryable errors (should not be retried)
        non_retryable_patterns = [
            "permission denied",
            "host key verification failed",
            "authentication failed",
            "repository not found",
            "not found",
            "does not exist",
            "invalid",
            "malformed",
            "fatal: not a git repository",
            "access denied",
        ]

        # Check for fatal file system errors after pack transfer
        if (
            "fatal: could not open" in error_output
            and "total" in error_output
            and "delta" in error_output
        ):
            # Only retryable if not a missing file error
            if "no such file or directory" in error_output:
                logger.debug(
                    f"Post-transfer missing file error (non-retryable): {error_output[:100]}..."
                )
                return False
            # Otherwise can be transient I/O stress - allow retries
            logger.debug(
                f"Post-transfer file error detected (retryable): {error_output[:100]}..."
            )
            return True

        if any(pattern in error_output for pattern in non_retryable_patterns):
            logger.debug(f"Non-retryable error detected: {error_output[:100]}...")
            return False

        # Retryable errors
        retryable_patterns = [
            "timeout",
            "connection refused",
            "connection timed out",
            "network",
            "temporary failure",
            "early eof",
            "remote end hung up",
            "transfer closed",
            "rpc failed",
            "could not resolve hostname",
            "ssh: connect to host",
            "connection reset",
            "could not lock config file",  # File locking is temporary and retryable (but check for missing files elsewhere)
        ]

        if any(pattern in error_output for pattern in retryable_patterns):
            logger.debug(f"Retryable error detected: {error_output[:100]}...")
            return True

        # For unknown errors, default to retryable but log it
        logger.warning(
            f"Unknown error pattern, defaulting to retryable: {error_output[:100]}..."
        )
        return True

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format.

        Args:
            seconds: Duration in seconds

        Returns:
            Formatted duration string
        """
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes}m"
        else:
            hours = int(seconds / 3600)
            return f"{hours}h"
