# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Cloning a GitHub repository through the ``gh`` CLI."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from gerrit_clone.github_clone_results import build_clone_result
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus
from gerrit_clone.subprocess_tracking import ProcessAbandonedError, run_tracked

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from gerrit_clone.models import CloneResult, Config, Project

logger = get_logger(__name__)


def _build_gh_clone_command(
    project: Project,
    config: Config,
    target_path: Path,
) -> list[str]:
    """Assemble the ``gh repo clone`` command line."""
    cmd = ["gh", "repo", "clone"]

    # Add repository identifier (org/repo or full URL)
    # gh CLI can handle both "org/repo" format and full URLs
    if project.clone_url and project.clone_url.startswith("http"):
        repo_identifier = project.clone_url
    else:
        repo_identifier = project.name

    cmd.append(repo_identifier)
    cmd.append(str(target_path))

    # Use --mirror for complete repository metadata (all refs, tags, branches)
    # This creates a bare repository that is a complete copy of the remote
    if config.mirror:
        cmd.extend(["--", "--mirror"])
    else:
        # Non-mirror mode: optionally use shallow clone or specific branch
        # Add depth for shallow clone
        if config.depth:
            cmd.extend(["--", "--depth", str(config.depth)])

        # Add branch if specified
        if config.branch:
            if "--" not in cmd:
                cmd.append("--")
            cmd.extend(["--branch", config.branch])

    return cmd


def _discard_partial_clone(target_path: Path) -> None:
    """Remove a partially written clone directory, ignoring errors."""
    if target_path.exists():
        shutil.rmtree(target_path, ignore_errors=True)


def clone_with_gh_cli(
    project: Project,
    config: Config,
    target_path: Path,
    started_at: datetime,
) -> CloneResult:
    """Clone repository using GitHub CLI.

    Args:
        project: Project to clone
        config: Configuration
        target_path: Target clone path
        started_at: Clone start time

    Returns:
        CloneResult
    """
    logger.debug(f"Cloning {project.name} with gh CLI")

    cmd = _build_gh_clone_command(project, config, target_path)

    try:
        logger.debug(f"Executing: {' '.join(cmd)}")
        # Tracked for the same reason the git path is: a batch that
        # gives up must be able to stop the child rather than wait for
        # it, and gh spawns a git transfer of its own that has to go
        # with it.  See gerrit_clone.subprocess_tracking.
        result = run_tracked(
            cmd,
            timeout=config.clone_timeout,
        )

        if result.returncode == 0:
            logger.debug(f"✓ Cloned {project.name} with gh CLI")
            return build_clone_result(
                project, target_path, started_at, CloneStatus.SUCCESS
            )

        error_msg = result.stderr.strip() or result.stdout.strip()
        logger.error(f"✗ Failed to clone {project.name}: {error_msg}")
        _discard_partial_clone(target_path)
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, error_msg
        )

    except ProcessAbandonedError:
        # The batch gave up before this clone could start.  Reporting it
        # as an ordinary failure would have the destination kept for a
        # clone that is never going to run, so it propagates.
        raise
    except subprocess.TimeoutExpired:
        error_msg = f"Clone timeout after {config.clone_timeout}s"
        logger.error(f"✗ {project.name}: {error_msg}")
        _discard_partial_clone(target_path)
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, error_msg
        )
    except Exception as e:
        error_msg = f"Clone error: {e}"
        logger.error(f"✗ {project.name}: {error_msg}")
        _discard_partial_clone(target_path)
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, error_msg
        )
