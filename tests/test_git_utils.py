# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for git_utils module."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from gerrit_clone.git_utils import (
    get_current_branch,
    get_current_commit_sha,
    get_remote_url,
    is_git_repository,
    is_repo_dirty,
)


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

    def test_clean_repository(self) -> None:
        """Test clean repository returns False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "repo"
            repo_path.mkdir()

            subprocess.run(
                ["git", "init"], cwd=repo_path, check=True, capture_output=True
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
