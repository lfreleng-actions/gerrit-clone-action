# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Clone URLs, isolated git environments and remote configuration.

Owns how a project is addressed as a git URL and the environment the clone
subprocess runs in.  Many clone workers run concurrently against the same
machine, so each one is given a private ``HOME`` and git config.  Without that
isolation the workers contend on the user's global ``.gitconfig`` and produce
spurious "could not lock config file" failures.
"""

from __future__ import annotations

import atexit
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger
from gerrit_clone.subprocess_tracking import (
    ProcessAbandonedError,
    batch_abandoned,
    current_generation,
    generation_abandoned,
    run_tracked,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from gerrit_clone.models import Config

logger = get_logger(__name__)

# One isolated git config directory per worker thread, reused by every
# clone that thread performs.  Its contents are identical for every
# clone, so creating one per clone *attempt* -- and deleting none --
# left thousands of ``git_config_*`` directories in the system temp
# directory over a large hierarchy, more again with retries.  Reuse
# bounds the count at the thread count for one operation, and
# ``isolated_git_config_scope`` removes them when that operation ends.
_thread_state = threading.local()
_isolated_config_dirs: set[Path] = set()
_quarantined_config_dirs: set[Path] = set()
#: Batch each registered directory belongs to, where it has one.  A
#: worker thread carries its pool's generation for life, so its
#: directory has exactly one owner.  Whether a directory may be removed
#: turns on that owner -- has *its* batch been abandoned? -- and not on
#: anything that has happened elsewhere in the process.
_config_dir_owners: dict[Path, int] = {}
#: May be held while :func:`generation_abandoned` takes the tracker's
#: lock, never the other way round: nothing in
#: :mod:`gerrit_clone.subprocess_tracking` calls back into this module,
#: so no thread waits for this lock while holding that one.
_isolated_config_lock = threading.Lock()
_active_operations = 0


def build_ssh_url(project_name: str, config: Config) -> str:
    """Build SSH URL for project.

    Args:
        project_name: Project to clone
        config: Configuration for clone operations

    Returns:
        SSH clone URL
    """
    user_prefix = f"{config.ssh_user}@" if config.ssh_user else ""
    return f"ssh://{user_prefix}{config.host}:{config.port}/{project_name}"


def build_https_url(project_name: str, config: Config) -> str:
    """Build HTTPS URL for project.

    Args:
        project_name: Project to clone
        config: Configuration for clone operations

    Returns:
        HTTPS clone URL
    """
    return f"{config.base_url}/{project_name}"


def create_isolated_git_config(config_dir: Path) -> None:
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


def isolated_git_config_dir() -> Path:
    """Return this thread's isolated git config directory.

    Created on first use and reused afterwards.  A directory removed by
    :func:`cleanup_isolated_git_configs` while the thread is idle is
    recreated on the next call.

    Returns:
        Directory to use as ``HOME`` for this thread's git subprocesses
    """
    existing: Path | None = getattr(_thread_state, "git_config_dir", None)
    if existing is not None and existing.is_dir():
        return existing

    config_dir = Path(tempfile.mkdtemp(prefix=f"git_config_{threading.get_ident()}_"))
    create_isolated_git_config(config_dir)
    _thread_state.git_config_dir = config_dir
    generation = current_generation()
    # Asked before taking the lock, not nested in it, and only to decide
    # where to register.  If the batch is abandoned in between, the
    # directory is registered with its owner and collection asks again.
    abandoned = generation is not None and generation_abandoned(generation)
    with _isolated_config_lock:
        # A worker from an abandoned pool can still reach here: it may
        # have been part-way through its pre-clone checks when the pool
        # shut down without waiting.  Registering it normally would put
        # a live HOME where the next completed operation would collect
        # it.  A worker of any other batch registers normally, however
        # many batches have been abandoned before it.
        if abandoned:
            _quarantined_config_dirs.add(config_dir)
        else:
            _isolated_config_dirs.add(config_dir)
            if generation is not None:
                _config_dir_owners[config_dir] = generation
    return config_dir


@contextmanager
def isolated_git_config_scope() -> Generator[None, None, None]:
    """Bracket a clone operation that uses isolated git config directories.

    The directories are removed when the **last** active operation
    leaves, never while another is still running: the registry is
    process-wide, so an unconditional cleanup would delete the ``HOME``
    and ``GIT_CONFIG_GLOBAL`` of a concurrent caller's live clones.

    A directory whose batch has been abandoned is quarantined instead.
    An abandoned pool shuts down without waiting -- on a timeout, and on
    the Ctrl+C that ``interruptible_executor`` turns into the same exit
    -- so its workers may still be cloning against these directories.
    Both routes record the abandonment before this scope exits, so it
    needs no signal of its own: the owner of each directory says whether
    it is still in use.  Quarantined directories are out of reach of
    every later scope and removed only at process exit.  That is bounded
    by the pool size of each abandoned batch; the workers of other
    batches, before or after it, are collected as usual.
    """
    global _active_operations  # noqa: PLW0603
    with _isolated_config_lock:
        _active_operations += 1

    try:
        yield
    finally:
        with _isolated_config_lock:
            _active_operations -= 1
            # Detached inside the same critical section as the
            # decrement.  Releasing the lock first would let a new
            # operation start and register its directory before the
            # snapshot was taken, and this one would then delete a live
            # HOME out from under it.
            expired = _detach_registry() if _active_operations == 0 else []
        _remove_config_dirs(expired)


def cleanup_isolated_git_configs() -> None:
    """Remove every isolated git config directory this process created.

    Safe to call directly.  Any thread that goes on to clone again gets
    a fresh directory.

    Deferred while any clone operation is active: those directories are
    live ``HOME`` / ``GIT_CONFIG_GLOBAL`` values, so deleting them out
    from under a running clone is the very race
    :func:`isolated_git_config_scope` exists to prevent.  The last scope
    to exit does the cleanup instead.

    Directories owned by an abandoned batch are quarantined rather than
    removed, and only :func:`_cleanup_at_exit` collects those.

    A directory that cannot be removed stays registered, so a later
    cleanup can retry it rather than forgetting it permanently.
    """
    with _isolated_config_lock:
        if _active_operations:
            logger.debug(
                f"Deferring isolated git config cleanup: "
                f"{_active_operations} clone operation(s) still active"
            )
            return
        expired = _detach_registry()
    _remove_config_dirs(expired)


def _cleanup_at_exit() -> None:
    """Remove everything, quarantined directories included.

    Registered with :mod:`atexit`.  Nothing else collects the
    quarantine, so this is the only thing standing between an abandoned
    batch and a directory left in the system temp directory.
    """
    with _isolated_config_lock:
        expired = _detach_registry()
        expired.extend(_quarantined_config_dirs)
        _quarantined_config_dirs.clear()
    _remove_config_dirs(expired)


def _detach_registry() -> list[Path]:
    """Take the registered directories that may now be removed.

    A directory whose owning batch has been abandoned is moved to the
    quarantine instead: that batch's workers may still be using it as
    ``HOME``.  Everything else is returned, the registry is cleared, and
    the ownership records go with it.

    The caller must hold ``_isolated_config_lock``.
    """
    removable: list[Path] = []
    for config_dir in _isolated_config_dirs:
        owner = _config_dir_owners.pop(config_dir, None)
        if owner is not None and generation_abandoned(owner):
            _quarantined_config_dirs.add(config_dir)
        else:
            removable.append(config_dir)
    _isolated_config_dirs.clear()
    return removable


def _remove_config_dirs(config_dirs: list[Path]) -> None:
    """Delete *config_dirs*, re-registering any that resist."""
    for config_dir in config_dirs:
        try:
            shutil.rmtree(config_dir)
        except FileNotFoundError:
            # Already gone; nothing to retry.
            continue
        except OSError as exc:
            logger.warning(f"Could not remove isolated git config {config_dir}: {exc}")
            with _isolated_config_lock:
                _isolated_config_dirs.add(config_dir)


atexit.register(_cleanup_at_exit)


def build_clone_environment(config: Config) -> dict[str, str]:
    """Build environment variables for git clone.

    Args:
        config: Configuration for clone operations

    Returns:
        Environment dictionary
    """
    env = os.environ.copy()

    # Set GIT_SSH_COMMAND for strict host checking
    if config.git_ssh_command:
        env["GIT_SSH_COMMAND"] = config.git_ssh_command

    git_config_dir = isolated_git_config_dir()

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


def _subprocess_error_text(error: BaseException) -> str:
    """Gather everything a failed git invocation reported.

    ``CalledProcessError.__str__`` is only ``Command '...' returned
    non-zero exit status N``.  Git writes the reason -- "could not lock
    config file" and friends -- to stderr, which ``capture_output=True``
    puts on ``stderr`` and never in the string form.  Classifying on
    ``str(error)`` alone therefore never matched.

    Args:
        error: Exception raised by the git invocation

    Returns:
        The exception text combined with any captured output
    """
    parts = [
        str(error),
        getattr(error, "stderr", None),
        getattr(error, "stdout", None),
    ]
    return " ".join(str(part) for part in parts if part)


def _is_config_lock_error(error_msg: str) -> bool:
    """Report whether a git failure looks like config-file lock contention.

    A bare "no such file or directory" is deliberately not enough.  Now
    that this sees real stderr rather than only the exception's string
    form, that substring matches all manner of permanent failures --
    which :mod:`gerrit_clone.clone_retry_policy` already classifies as
    non-retryable -- so it must be tied to the config file to count.

    Args:
        error_msg: Error text from the failed git invocation

    Returns:
        True if the failure is worth retrying after a short delay
    """
    lowered = error_msg.lower()
    if "could not lock config file" in lowered:
        return True
    return ".git/config" in lowered and (
        "could not open" in lowered or "no such file or directory" in lowered
    )


def set_ssh_remote(
    project_name: str, repo_path: Path, ssh_url: str, env: dict[str, str]
) -> None:
    """Set the remote URL to SSH after HTTPS clone with isolated environment.

    Args:
        project_name: Project that was cloned (for logging)
        repo_path: Path to the cloned repository
        ssh_url: SSH URL to point the origin remote at
        env: Isolated git environment to use
    """
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            # Tracked like the clone itself: a batch that gives up must
            # be able to stop this too, or it outlives the batch and
            # races the cleanup of the directory it is working in.
            result = run_tracked(
                ["git", "remote", "set-url", "origin", ssh_url],
                cwd=repo_path,
                timeout=10,
                env=env,  # Use isolated environment
            )
            if result.returncode != 0:
                # A terminated command carries a negative return code,
                # which is indistinguishable from git having failed on
                # its own.  Only the tracker knows the difference.
                if batch_abandoned():
                    raise ProcessAbandonedError(
                        f"Setting the SSH remote for {project_name} was abandoned"
                    )
                raise subprocess.CalledProcessError(
                    result.returncode,
                    result.args,
                    output=result.stdout,
                    stderr=result.stderr,
                )
            logger.debug(
                f"Set SSH remote for [project]{project_name}[/project]: {ssh_url}"
            )
            return
        except ProcessAbandonedError:
            # Every other failure here is reported and shrugged off, the
            # clone itself having worked.  This one is not: the batch
            # has given up, and swallowing it would have the worker
            # report success for a repository still on HTTPS, which the
            # timeout cleanup would then keep.
            raise
        except subprocess.SubprocessError as e:
            error_text = _subprocess_error_text(e)
            if not _is_config_lock_error(error_text):
                logger.warning(
                    f"Failed to set SSH remote for [project]{project_name}[/project]: {error_text}"
                )
                return
            if attempt >= max_attempts:
                logger.warning(
                    f"Failed to set SSH remote for [project]{project_name}[/project] after {max_attempts} attempts: {error_text}"
                )
                return
            # Small delay with jitter for config lock retry
            delay = 0.2 + (random.uniform(0.1, 0.3) * attempt)
            logger.debug(
                f"Config lock detected for {project_name}, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_attempts})"
            )
            time.sleep(delay)
            continue
        except Exception as e:
            logger.warning(
                f"Unexpected error setting SSH remote for [project]{project_name}[/project]: {e}"
            )
            return
