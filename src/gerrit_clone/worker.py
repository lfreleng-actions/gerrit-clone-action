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
from gerrit_clone.pathing import check_path_conflicts, get_project_path, move_conflicting_path

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

    def __init__(self, config: Config, project_index: set[str] | None = None) -> None:
        """Initialize clone worker.

        Args:
            config: Configuration for clone operations
            project_index: Set of all project names (for accurate ancestor detection)
        """
        self.config = config
        self._project_index = project_index or set()
        # Track whether we attempted late ancestor detection
        self._late_nested_checks: int = 0

    def clone_project(self, project: Project) -> CloneResult:
        """Clone a single project repository.

        Note: With dependency-aware batching, parent/child conflicts are eliminated
        by architectural design, so complex locking is no longer needed.

        Args:
            project: Project to clone

        Returns:
            CloneResult with operation details
        """
        logger.debug(f"🔄 Processing {project.name}")
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
            logger.debug(f"📁 Processing {project.name}")
            depth = project.name.count("/")

            # Optional nested ancestor detection (supports BOTH policy)
            def _find_git_ancestor(p: Path) -> Path | None:
                cur = p.parent
                while cur != cur.parent:
                    if (cur / ".git").is_dir():
                        return cur
                    cur = cur.parent
                return None

            # Corrected ancestor detection: only treat a directory as a parent if it is itself
            # a cloned project (its relative path exists in project index) and we are not
            # crossing above the configured path prefix.
            def _find_project_git_ancestor(p: Path) -> Path | None:
                try:
                    base = self.config.path_prefix.resolve()
                    current = p.parent.resolve()
                except OSError:
                    return None
                while True:
                    if current == base:
                        # Do not treat the workspace root as a project ancestor
                        return None
                    try:
                        rel = current.relative_to(base)
                    except ValueError:
                        # Stepped outside base
                        return None
                    rel_str = rel.as_posix()
                    if rel_str in self._project_index and (current / ".git").is_dir():
                        return current
                    if current == current.parent:
                        return None
                    current = current.parent

            ancestor_repo: Path | None = _find_project_git_ancestor(target_path)

            # Handle nested repositories (always clone both parent and children)
            allow_nested = getattr(self.config, "allow_nested_git", False)
            nested_protection = getattr(self.config, "nested_protection", False)

            if ancestor_repo and not allow_nested:
                # Treat nesting as failure if not allowed
                result.status = CloneStatus.FAILED
                result.error_message = f"Nested clone forbidden (ancestor git repo at {ancestor_repo.name})"
                result.completed_at = datetime.now(UTC)
                result.duration_seconds = (result.completed_at - started_at).total_seconds()
                logger.error(f"❌ Nested clone blocked for {project.name} (ancestor={ancestor_repo.name}, allow_nested_git=False)")
                return result

            if ancestor_repo and allow_nested:
                # Annotate intended nesting relationship early with relative path under base
                try:
                    rel = ancestor_repo.relative_to(self.config.path_prefix)
                    result.nested_under = rel.as_posix()
                    logger.debug(f"🧬 Nested repo detected early: {project.name} (parent={result.nested_under})")
                except Exception:
                    # Fallback to directory name
                    result.nested_under = ancestor_repo.name
                    logger.debug(f"🧬 Nested repo detected early (fallback): {project.name} (parent={result.nested_under})")
            elif depth > 0:
                logger.debug(f"No early ancestor detected for candidate nested project {project.name} (depth={depth})")

            # Check for path conflicts at the precise target
            is_nested = result.nested_under is not None
            conflict = check_path_conflicts(target_path, is_nested_repo=is_nested)
            if conflict is not None:
                if conflict == "already_cloned":
                    result.status = CloneStatus.ALREADY_EXISTS
                    result.completed_at = datetime.now(UTC)
                    result.duration_seconds = (
                        result.completed_at - started_at
                    ).total_seconds()
                    logger.debug(f"✓ Repository {project.name} already exists - skipped")
                    return result
                elif conflict == "incomplete_clone":
                    # Handle content conflict - could be incomplete clone or nested repo content from parent
                    if result.nested_under:
                        logger.info(f"🧹 Replacing parent repository content with nested repository for {project.name}")
                    else:
                        logger.warning(f"🧹 Cleaning up incomplete clone for {project.name}")
                    try:
                        import shutil
                        shutil.rmtree(target_path)
                        logger.debug(f"✓ Cleaned up incomplete clone directory: {target_path}")
                    except Exception as cleanup_error:
                        result.status = CloneStatus.FAILED
                        result.error_message = f"Failed to cleanup incomplete clone: {cleanup_error}"
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.error(f"Cleanup failed for {project.name}: {cleanup_error}")
                        return result
                    # Continue with normal clone after cleanup
                elif conflict == "nested_file_conflict":
                    # Handle nested repo file conflict
                    move_conflicting_enabled = getattr(self.config, "move_conflicting", True)
                    if move_conflicting_enabled:
                        # Try to move the conflicting file/directory
                        try:
                            if move_conflicting_path(target_path, is_nested_repo=True):
                                parent_name = result.nested_under or "parent"
                                logger.warning(f"⚠️ Moved conflicting content in parent repository '{parent_name}' to allow cloning of nested repository [project]{project.name}[/project]")
                                # Continue with normal clone after moving conflict
                            else:
                                # Move failed, skip gracefully
                                result.status = CloneStatus.SKIPPED
                                result.error_message = f"Skipped due to file conflict with parent repository (move failed)"
                                result.completed_at = datetime.now(UTC)
                                result.duration_seconds = (
                                    result.completed_at - started_at
                                ).total_seconds()
                                parent_name = result.nested_under or "parent"
                                logger.warning(f"⚠️ Skipping nested repository [project]{project.name}[/project]: "
                                             f"Parent repository '{parent_name}' contains a file that conflicts with nested directory structure (could not move)")
                                return result
                        except Exception as move_error:
                            # Move failed with exception, skip gracefully
                            result.status = CloneStatus.SKIPPED
                            result.error_message = f"Skipped due to file conflict with parent repository (move error: {move_error})"
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            parent_name = result.nested_under or "parent"
                            logger.warning(f"⚠️ Skipping nested repository [project]{project.name}[/project]: "
                                         f"Parent repository '{parent_name}' contains a file that conflicts with nested directory structure (move failed: {move_error})")
                            return result
                    else:
                        # Move conflicting disabled, skip gracefully
                        result.status = CloneStatus.SKIPPED
                        result.error_message = f"Skipped due to file conflict with parent repository"
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        parent_name = result.nested_under or "parent"
                        logger.warning(f"⚠️ Skipping nested repository [project]{project.name}[/project]: "
                                     f"Parent repository '{parent_name}' contains a file that conflicts with nested directory structure")
                        return result
                else:
                    result.status = CloneStatus.FAILED
                    result.error_message = f"Path conflict: {conflict}"
                    result.completed_at = datetime.now(UTC)
                    result.duration_seconds = (
                        result.completed_at - started_at
                    ).total_seconds()
                    logger.error(f"Path conflict for [project]{project.name}[/project]: {conflict}")
                    return result

            # Ensure parent directories exist (safe due to dependency batching)
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # If nested and protection enabled, add child path to parent exclude
            if ancestor_repo and allow_nested and nested_protection:
                try:
                    rel_child = target_path.relative_to(ancestor_repo)
                    exclude_file = ancestor_repo / ".git" / "info" / "exclude"
                    exclude_file.parent.mkdir(parents=True, exist_ok=True)
                    existing_lines: list[str] = []
                    if exclude_file.exists():
                        existing_lines = exclude_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if str(rel_child) not in existing_lines:
                        with exclude_file.open("a", encoding="utf-8") as ef:
                            ef.write(f"\n# auto-added to ignore nested repo\n{rel_child}\n")
                        logger.debug(f"Added nested protection exclude entry for {project.name} under {result.nested_under}")
                    else:
                        logger.debug(f"Nested protection exclude already present for {project.name}")
                except Exception as e:
                    logger.warning(f"Could not apply nested protection for {project.name}: {e}")

            # Update status to cloning
            result.status = CloneStatus.CLONING

            # Instrumentation: mark potential nested candidate if depth > 0 and still no ancestor
            if depth > 0 and result.nested_under is None:
                logger.debug(f"Nested candidate (no parent yet): {project.name} (will re-check before clone subprocess)")

            # Perform clone with adaptive retry
            logger.debug(f"Starting clone execution for {project.name}")
            success = self._execute_adaptive_clone(project, target_path, result)
            logger.debug(f"Clone execution completed for {project.name}, success: {success}")

            # Handle success/failure
            if success:
                result.status = CloneStatus.SUCCESS
                if ancestor_repo and allow_nested:
                    logger.debug(f"📚 Nested clone succeeded: {project.name} (ancestor={ancestor_repo.name})")
            else:
                result.status = CloneStatus.FAILED
                if not result.error_message:
                    result.error_message = "Clone failed for unknown reason"

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
                    logger.debug(
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
        logger.debug(
            f"⬇️ Cloning {project.name} (attempt {result.attempts}/{self.config.retry_policy.max_attempts})"
        )
        logger.debug(
            f"Cloning [project]{project.name}[/project] (attempt {result.attempts})"
        )
        logger.debug(f"Clone command: {' '.join(cmd)}")

        try:
            logger.debug(f"🔧 Executing git clone for {project.name}")
            logger.debug(f"Starting clone subprocess for {project.name}")
            start_time = datetime.now(UTC)

            pre_late_nested = result.nested_under

            # Late ancestor re-check: parent may have finished cloning after initial worker start
            if getattr(self.config, "allow_nested_git", False) and result.nested_under is None:
                self._late_nested_checks += 1
                try:
                    # Re-run ancestor detection using the same logic as earlier
                    base = self.config.path_prefix.resolve()
                    current = target_path.parent.resolve()
                    while True:
                        if current == base:
                            break
                        try:
                            rel = current.relative_to(base)
                        except ValueError:
                            break
                        rel_str = rel.as_posix()
                        if rel_str in (self._project_index or set()) and (current / ".git").is_dir():
                            result.nested_under = rel_str
                            logger.debug(f"Late ancestor detection: {project.name} nested under {rel_str}")
                            # Apply nested protection if enabled and not already applied
                            if getattr(self.config, "nested_protection", False):
                                try:
                                    exclude_file = current / ".git" / "info" / "exclude"
                                    exclude_file.parent.mkdir(parents=True, exist_ok=True)
                                    rel_child = target_path.relative_to(current)
                                    existing_lines: list[str] = []
                                    if exclude_file.exists():
                                        existing_lines = exclude_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                                    if str(rel_child) not in existing_lines:
                                        with exclude_file.open("a", encoding="utf-8") as ef:
                                            ef.write(f"\n# auto-added (late) to ignore nested repo\n{rel_child}\n")
                                        logger.debug(f"🧬 Nested repo detected late: {project.name} (parent={result.nested_under})")
                                except Exception as ne:
                                    logger.debug(f"Late nested protection application failed for {project.name}: {ne}")
                            break
                        if current == current.parent:
                            break
                        current = current.parent
                except Exception as late_e:
                    logger.debug(f"Late ancestor re-check failed for {project.name}: {late_e}")
                if pre_late_nested is None and result.nested_under is None and project.name.count("/") > 0:
                    logger.debug(f"No ancestor after late re-check: {project.name} (treat as top-level clone)")

            # Execute git clone directly to target path - Git handles its own atomicity
            process_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=self.config.clone_timeout,
                env=env,
                cwd=self.config.path_prefix,
                check=False,
            )
            # If SSH debug enabled and clone failed, log raw stderr/stdout (truncated) before analysis
            if getattr(self.config, "ssh_debug", False) and process_result.returncode != 0:
                raw_stderr = (process_result.stderr or "").strip()
                raw_stdout = (process_result.stdout or "").strip()
                max_len = 1200
                if len(raw_stderr) > max_len:
                    raw_stderr_display = raw_stderr[:max_len] + "...(truncated)"
                else:
                    raw_stderr_display = raw_stderr
                if len(raw_stdout) > max_len:
                    raw_stdout_display = raw_stdout[:max_len] + "...(truncated)"
                else:
                    raw_stdout_display = raw_stdout
                logger.debug(f"[ssh-debug][raw-stderr] {raw_stderr_display}")
                if raw_stdout_display:
                    logger.debug(f"[ssh-debug][raw-stdout] {raw_stdout_display}")

            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            logger.debug(
                f"Clone subprocess completed for {project.name} in {duration:.1f}s"
            )

            if process_result.returncode == 0:
                # Set SSH remote if requested and we cloned with HTTPS
                if self.config.use_https and not self.config.keep_remote_protocol:
                    self._set_ssh_remote(project, target_path, env)

                logger.debug(f"✅ Successfully cloned {project.name}")
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
                    encoding='utf-8',
                    errors='replace',
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

        # Common error patterns with enhanced debugging info
        if "Permission denied" in error_output:
            # Extract more context from SSH errors
            ssh_user = getattr(self.config, 'ssh_user', 'git')
            identity_file = getattr(self.config, 'ssh_identity_file', 'default')
            return f"Permission denied - SSH auth failed for {ssh_user}@{self.config.host}:{self.config.port} (key: {identity_file}) accessing {project_name}"
        elif "Host key verification failed" in error_output:
            return (
                f"Host key verification failed for {self.config.host} - run: ssh-keyscan -p {self.config.port} {self.config.host} >> ~/.ssh/known_hosts"
            )
        elif "Connection refused" in error_output:
            return f"Connection refused - check if SSH service is running on {self.config.host}:{self.config.port}"
        elif "could not resolve hostname" in error_output.lower():
            return f"DNS resolution failed - cannot resolve {self.config.host}"
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
        elif "timeout" in error_output.lower() or "timed out" in error_output.lower():
            return f"Network timeout during clone (timeout: {self.config.clone_timeout}s) - consider increasing --clone-timeout"
        elif "too many open files" in error_output.lower():
            return "Resource exhaustion: too many open files - reduce --threads or increase system limits"
        elif "no space left" in error_output.lower():
            return "Disk space exhausted - check available disk space"
        elif "connection reset" in error_output.lower():
            return "Network connection reset - possible network instability or rate limiting"
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
