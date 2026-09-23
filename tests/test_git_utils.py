# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for git_utils module."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from gerrit_clone.git_utils import (
    get_current_branch,
    get_current_commit_sha,
    get_head_ref,
    get_remote_url,
    is_gerrit_parent_project,
    is_git_repository,
    is_repo_dirty,
    list_local_branches,
)
from gerrit_clone.subprocess_tracking import (
    ProcessAbandonedError,
    _thread_state,
    enter_generation,
    new_generation,
    refuse_generation,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class TestIsGitRepository:
    """Test is_git_repository function."""

    def test_non_existent_path(self) -> None:
        """Test with non-existent path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            non_existent = Path(temp_dir) / "does-not-exist"
            assert is_git_repository(non_existent) is False

    def test_file_path(self) -> None:
        """Test with a file (not a directory)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("test")
            assert is_git_repository(test_file) is False

    def test_empty_directory(self) -> None:
        """Test with empty directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_dir = Path(temp_dir) / "empty"
            empty_dir.mkdir()
            assert is_git_repository(empty_dir) is False

    def test_regular_git_repository(self) -> None:
        """Test detection of regular git repository."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "regular-repo"
            repo_path.mkdir()

            # Initialize a regular git repository
            subprocess.run(
                ["git", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            assert is_git_repository(repo_path) is True

    def test_bare_git_repository(self) -> None:
        """Test detection of bare git repository."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bare_repo_path = Path(temp_dir) / "bare-repo.git"
            bare_repo_path.mkdir()

            # Initialize a bare git repository
            subprocess.run(
                ["git", "init", "--bare"],
                cwd=bare_repo_path,
                check=True,
                capture_output=True,
            )

            assert is_git_repository(bare_repo_path) is True

    def test_mirror_cloned_repository(self) -> None:
        """Test detection of mirror-cloned repository (bare repo)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a source repository
            source_repo = Path(temp_dir) / "source"
            source_repo.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=source_repo,
                check=True,
                capture_output=True,
            )

            # Create a commit in source repo
            test_file = source_repo / "test.txt"
            test_file.write_text("test content")
            subprocess.run(
                ["git", "add", "test.txt"],
                cwd=source_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
                cwd=source_repo,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )

            # Mirror clone the repository
            mirror_repo = Path(temp_dir) / "mirror"
            subprocess.run(
                ["git", "clone", "--mirror", str(source_repo), str(mirror_repo)],
                check=True,
                capture_output=True,
            )

            assert is_git_repository(mirror_repo) is True

    def test_directory_with_git_like_files(self) -> None:
        """Test directory with git-like files but not a valid repo."""
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_repo = Path(temp_dir) / "fake"
            fake_repo.mkdir()

            # Create some files that look like git files but aren't a valid repo
            (fake_repo / "HEAD").write_text("fake head")
            (fake_repo / "config").write_text("fake config")

            # Should return False because it's not a complete/valid git repo
            # (missing objects/ and refs/ directories)
            assert is_git_repository(fake_repo) is False

    def test_incomplete_bare_repository(self) -> None:
        """Test incomplete bare repository (missing required directories)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            incomplete = Path(temp_dir) / "incomplete"
            incomplete.mkdir()

            # Create only some bare repo markers
            (incomplete / "HEAD").write_text("ref: refs/heads/main\n")
            (incomplete / "config").write_text("[core]\n\tbare = true\n")
            # Missing objects/ and refs/ directories

            assert is_git_repository(incomplete) is False


class TestIsGitRepositoryInAbandonedBatch:
    """The git fallback is a child like any other.

    Clone workers reach it before they reserve anything, so a worker
    that outlasts its batch's settle wait would otherwise start an
    untracked git after the batch has given up and even returned.
    """

    @pytest.fixture
    def abandoned(self) -> Generator[None, None, None]:
        """Bind the calling thread to a batch that has given up."""
        generation = new_generation()
        enter_generation(generation)
        refuse_generation(generation)
        try:
            yield
        finally:
            _thread_state.generation = None

    @staticmethod
    def _not_a_repository(tmp_path: Path) -> Path:
        """A directory only the git fallback can classify."""
        directory = tmp_path / "not-a-repo"
        directory.mkdir()
        (directory / "HEAD").write_text("fake head")
        return directory

    @pytest.mark.usefixtures("abandoned")
    def test_the_probe_is_refused(self, tmp_path: Path) -> None:
        directory = self._not_a_repository(tmp_path)
        with (
            patch("gerrit_clone.subprocess_tracking.subprocess.Popen") as popen,
            pytest.raises(ProcessAbandonedError),
        ):
            is_git_repository(directory)
        assert not popen.called

    @pytest.mark.usefixtures("abandoned")
    def test_a_terminated_probe_is_not_read_as_no_repository(
        self, tmp_path: Path
    ) -> None:
        """Terminated, git exits non-zero, as a plain directory would."""
        directory = self._not_a_repository(tmp_path)
        terminated = subprocess.CompletedProcess(["git"], -15, "", "")
        with (
            patch("gerrit_clone.git_utils.run_tracked", return_value=terminated),
            pytest.raises(ProcessAbandonedError),
        ):
            is_git_repository(directory)


class TestGetCurrentCommitSha:
    """Test get_current_commit_sha function."""

    def test_regular_repository_with_commits(self) -> None:
        """Test getting SHA from regular repository with commits."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()

            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
            )

            # Create a commit
            test_file = repo_path / "test.txt"
            test_file.write_text("test")
            subprocess.run(
                ["git", "add", "test.txt"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Test commit"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )

            sha = get_current_commit_sha(repo_path)
            assert sha is not None
            assert len(sha) == 40  # Full SHA is 40 hex characters
            assert all(c in "0123456789abcdef" for c in sha)

    def test_bare_repository_with_commits(self) -> None:
        """Test getting SHA from bare repository with commits."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create source repo
            source = Path(temp_dir) / "source"
            source.mkdir()
            subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)

            test_file = source / "test.txt"
            test_file.write_text("test")
            subprocess.run(
                ["git", "add", "test.txt"], cwd=source, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Test"],
                cwd=source,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )

            # Mirror clone
            bare = Path(temp_dir) / "bare"
            subprocess.run(
                ["git", "clone", "--mirror", str(source), str(bare)],
                check=True,
                capture_output=True,
            )

            sha = get_current_commit_sha(bare)
            assert sha is not None
            assert len(sha) == 40

    def test_non_existent_path(self) -> None:
        """Test with non-existent path raises FileNotFoundError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            non_existent = Path(temp_dir) / "does-not-exist"
            with pytest.raises(FileNotFoundError):
                get_current_commit_sha(non_existent)

    def test_not_a_git_repository(self) -> None:
        """Test with non-git directory raises ValueError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            not_repo = Path(temp_dir) / "not-a-repo"
            not_repo.mkdir()
            with pytest.raises(ValueError, match="Not a git repository"):
                get_current_commit_sha(not_repo)

    def test_empty_repository(self) -> None:
        """Test empty repository with no commits returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "empty-repo"
            repo_path.mkdir()
            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
            )

            sha = get_current_commit_sha(repo_path)
            assert sha is None


class TestGetCurrentBranch:
    """Test get_current_branch function."""

    def test_regular_repository_with_branch(self) -> None:
        """Test getting branch from regular repository."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()

            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Create a commit
            test_file = repo_path / "test.txt"
            test_file.write_text("test")
            subprocess.run(
                ["git", "add", "test.txt"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Test"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )

            branch = get_current_branch(repo_path)
            assert branch == "main"

    def test_not_a_git_repository(self) -> None:
        """Test with non-git directory raises ValueError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            not_repo = Path(temp_dir) / "not-a-repo"
            not_repo.mkdir()
            with pytest.raises(ValueError, match="Not a git repository"):
                get_current_branch(not_repo)


class TestIsRepoDirty:
    """Test is_repo_dirty function."""

    def test_clean_repository(self, tmp_path: Path) -> None:
        """Test clean repository returns False."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        subprocess.run(
            ["git", "-c", "init.defaultBranch=main", "init"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        # Set minimal local config to avoid global config side effects
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        assert is_repo_dirty(repo_path) is False

    def test_dirty_repository(self) -> None:
        """Test repository with uncommitted changes returns True."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()

            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
            )

            # Create an uncommitted file
            test_file = repo_path / "test.txt"
            test_file.write_text("test")

            assert is_repo_dirty(repo_path) is True

    def test_bare_repository_never_dirty(self) -> None:
        """Test bare repository always returns False (cannot have working changes)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bare_repo = Path(temp_dir) / "bare.git"
            bare_repo.mkdir()
            subprocess.run(
                ["git", "init", "--bare"],
                cwd=bare_repo,
                check=True,
                capture_output=True,
            )

            # Bare repos cannot be dirty
            assert is_repo_dirty(bare_repo) is False


class TestGetRemoteUrl:
    """Test get_remote_url function."""

    def test_repository_with_remote(self) -> None:
        """Test getting remote URL from repository."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()

            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "remote", "add", "origin", "https://example.com/repo.git"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            url = get_remote_url(repo_path)
            assert url == "https://example.com/repo.git"

    def test_bare_repository_with_remote(self) -> None:
        """Test getting remote URL from bare repository."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create source with remote
            source = Path(temp_dir) / "source"
            source.mkdir()
            subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://example.com/source.git"],
                cwd=source,
                check=True,
                capture_output=True,
            )

            # Create commit
            test_file = source / "test.txt"
            test_file.write_text("test")
            subprocess.run(
                ["git", "add", "test.txt"], cwd=source, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Test"],
                cwd=source,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )

            # Mirror clone
            bare = Path(temp_dir) / "bare"
            subprocess.run(
                ["git", "clone", "--mirror", str(source), str(bare)],
                check=True,
                capture_output=True,
            )

            # Bare repos cloned with --mirror have the source as origin
            url = get_remote_url(bare)
            assert url is not None
            assert str(source) in url

    def test_repository_without_remote(self) -> None:
        """Test repository without remote returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
            )

            url = get_remote_url(repo_path)
            assert url is None

    def test_not_a_git_repository(self) -> None:
        """Test with non-git directory raises ValueError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            not_repo = Path(temp_dir) / "not-a-repo"
            not_repo.mkdir()
            with pytest.raises(ValueError, match="Not a git repository"):
                get_remote_url(not_repo)


class TestGetHeadRef:
    """Test get_head_ref function."""

    def test_regular_repo_with_branch(self) -> None:
        """Test reading HEAD ref from a regular repo on a branch."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )
            ref = get_head_ref(repo_path)
            assert ref == "refs/heads/main"

    def test_bare_repo_with_branch(self) -> None:
        """Test reading HEAD ref from a bare repo."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a source repo, then clone it bare
            source = Path(temp_dir) / "source"
            source.mkdir()
            subprocess.run(
                ["git", "init", "-b", "master"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=source,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )
            bare = Path(temp_dir) / "bare.git"
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(bare)],
                check=True,
                capture_output=True,
            )
            ref = get_head_ref(bare)
            assert ref == "refs/heads/master"

    def test_head_pointing_to_meta_config(self) -> None:
        """Test reading HEAD when it points to refs/meta/config (Gerrit parent project)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Simulate a bare repo whose HEAD points to refs/meta/config
            repo_path = Path(temp_dir) / "parent.git"
            repo_path.mkdir()
            (repo_path / "HEAD").write_text("ref: refs/meta/config\n")
            ref = get_head_ref(repo_path)
            assert ref == "refs/meta/config"

    def test_detached_head(self) -> None:
        """Test that a detached HEAD (raw SHA) returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            (repo_path / "HEAD").write_text("abc123def456\n")
            ref = get_head_ref(repo_path)
            assert ref is None

    def test_non_existent_path(self) -> None:
        """Test that a missing path returns None."""
        ref = get_head_ref(Path("/tmp/does-not-exist-ever-12345"))
        assert ref is None

    def test_non_bare_repo_via_dot_git(self) -> None:
        """Test that get_head_ref falls back to .git/HEAD for non-bare repos."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            subprocess.run(
                ["git", "init", "-b", "develop"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )
            ref = get_head_ref(repo_path)
            assert ref == "refs/heads/develop"


class TestListLocalBranches:
    """Test list_local_branches function."""

    def test_repo_with_multiple_branches(self) -> None:
        """Test listing branches from a repo with several branches."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env=env,
            )
            subprocess.run(
                ["git", "branch", "develop"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "branch", "feature-x"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            branches = list_local_branches(repo_path)
            assert branches == ["develop", "feature-x", "main"]

    def test_bare_repo_with_branches(self) -> None:
        """Test listing branches from a bare clone."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            source.mkdir()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            subprocess.run(
                ["git", "init", "-b", "master"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=source,
                check=True,
                capture_output=True,
                env=env,
            )
            subprocess.run(
                ["git", "branch", "release"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            bare = Path(temp_dir) / "bare.git"
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(bare)],
                check=True,
                capture_output=True,
            )
            branches = list_local_branches(bare)
            assert "master" in branches
            assert "release" in branches

    def test_repo_with_no_branches(self) -> None:
        """Test that a fake repo with no refs/heads returns empty list."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Simulate a bare repo that has only refs/meta/config
            repo_path = Path(temp_dir) / "parent.git"
            repo_path.mkdir()
            for d in ["objects", "refs", "refs/meta"]:
                (repo_path / d).mkdir(parents=True, exist_ok=True)
            (repo_path / "HEAD").write_text("ref: refs/meta/config\n")
            (repo_path / "config").write_text(
                "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            )
            branches = list_local_branches(repo_path)
            assert branches == []

    def test_non_existent_path(self) -> None:
        """Test that a missing path returns empty list."""
        branches = list_local_branches(Path("/tmp/does-not-exist-ever-12345"))
        assert branches == []

    def test_branches_are_sorted(self) -> None:
        """Test that returned branches are sorted alphabetically."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            subprocess.run(
                ["git", "init", "-b", "zebra"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env=env,
            )
            subprocess.run(
                ["git", "branch", "alpha"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "branch", "middle"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            branches = list_local_branches(repo_path)
            assert branches == ["alpha", "middle", "zebra"]


class TestIsGerritParentProject:
    """Test is_gerrit_parent_project function."""

    def test_gerrit_parent_project(self) -> None:
        """Test detection of a Gerrit parent project (HEAD -> refs/meta/config, no branches)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "parent.git"
            repo_path.mkdir()
            for d in ["objects", "refs", "refs/meta"]:
                (repo_path / d).mkdir(parents=True, exist_ok=True)
            (repo_path / "HEAD").write_text("ref: refs/meta/config\n")
            (repo_path / "config").write_text(
                "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            )
            assert is_gerrit_parent_project(repo_path) is True

    def test_normal_repo_with_branches(self) -> None:
        """Test that a normal repo with branches is NOT detected as parent project."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env=env,
            )
            assert is_gerrit_parent_project(repo_path) is False

    def test_meta_config_head_with_branches(self) -> None:
        """Test repo with HEAD -> refs/meta/config but that also has real branches.

        This can happen when a Gerrit project has both metadata and code
        branches (e.g. testsuite/pythonsdk-tests). It should NOT be
        classified as a parent project.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a real repo, then manually rewrite HEAD to refs/meta/config
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            subprocess.run(
                ["git", "init", "-b", "master"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env=env,
            )
            # Overwrite HEAD to point to refs/meta/config
            (repo_path / ".git" / "HEAD").write_text("ref: refs/meta/config\n")
            # Repo still has refs/heads/master, so it should NOT be a parent project
            assert is_gerrit_parent_project(repo_path) is False

    def test_non_existent_path(self) -> None:
        """Test that a missing path returns False."""
        assert is_gerrit_parent_project(Path("/tmp/does-not-exist-ever-12345")) is False

    def test_head_on_normal_branch(self) -> None:
        """Test that HEAD pointing to refs/heads/main is not a parent project."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo.git"
            repo_path.mkdir()
            (repo_path / "HEAD").write_text("ref: refs/heads/main\n")
            assert is_gerrit_parent_project(repo_path) is False

    def test_detached_head(self) -> None:
        """Test that a detached HEAD (raw SHA) is not a parent project."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo.git"
            repo_path.mkdir()
            (repo_path / "HEAD").write_text("abc123def456789\n")
            assert is_gerrit_parent_project(repo_path) is False
