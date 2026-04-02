# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for content filtering: file removal and token replacement."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gerrit_clone.content_filter import (
    _generate_replacement_string,
    _remove_files_filter_repo,
    apply_content_filters,
    match_file_pattern,
    normalize_file_patterns,
    parse_git_filter_spec,
    remove_files_from_bare_repo,
    replace_tokens_in_history,
)

# ---------------------------------------------------------------------------
# match_file_pattern tests
# ---------------------------------------------------------------------------


class TestMatchFilePattern:
    """Tests for the match_file_pattern utility."""

    # -- exact match --------------------------------------------------------

    def test_exact_match(self) -> None:
        """Exact file path matches."""
        assert match_file_pattern(".github/dependabot.yml", ".github/dependabot.yml")

    def test_no_match_different_path(self) -> None:
        """Different paths do not match."""
        assert not match_file_pattern("README.md", ".github/dependabot.yml")

    # -- glob wildcards -----------------------------------------------------

    def test_glob_star(self) -> None:
        """Star wildcard matches within a directory."""
        assert match_file_pattern(".github/dependabot.yml", ".github/*.yml")

    def test_glob_star_crosses_dirs(self) -> None:
        """fnmatch * matches across directory separators."""
        assert match_file_pattern(".github/workflows/ci.yml", ".github/*.yml")

    def test_glob_double_star(self) -> None:
        """Double star matches recursively."""
        assert match_file_pattern(".github/workflows/ci.yml", ".github/**")

    def test_glob_double_star_root(self) -> None:
        """Double star from root matches everything."""
        assert match_file_pattern("any/deep/path/file.txt", "**/*.txt")

    def test_glob_question_mark(self) -> None:
        """Question mark matches single character."""
        assert match_file_pattern("file.txt", "file.tx?")

    # -- suffix matching (multi-component patterns) -------------------------

    def test_suffix_match(self) -> None:
        """Multi-component pattern matches as suffix."""
        assert match_file_pattern(
            "prefix/.github/dependabot.yml", ".github/dependabot.yml"
        )

    # -- single-component patterns ------------------------------------------

    def test_single_component_any_segment(self) -> None:
        """Single pattern matches any path segment."""
        assert match_file_pattern("some/path/dependabot.yml", "dependabot.yml")

    def test_single_component_glob(self) -> None:
        """Single glob pattern matches filename."""
        assert match_file_pattern("path/to/file.pyc", "*.pyc")

    # -- regex patterns -----------------------------------------------------

    def test_regex_match(self) -> None:
        """Regex-prefixed pattern matches."""
        assert match_file_pattern("src/config.py", r"regex:\.py$")

    def test_regex_no_match(self) -> None:
        """Regex that does not match."""
        assert not match_file_pattern("README.md", r"regex:\.py$")

    # -- edge cases ---------------------------------------------------------

    def test_empty_pattern(self) -> None:
        """Empty pattern does not match."""
        assert not match_file_pattern("file.txt", "")

    def test_empty_path(self) -> None:
        """Empty path does not match."""
        assert not match_file_pattern("", "*.txt")


# ---------------------------------------------------------------------------
# normalize_file_patterns tests
# ---------------------------------------------------------------------------


class TestNormalizeFilePatterns:
    """Tests for the normalize_file_patterns helper."""

    def test_comma_separated(self) -> None:
        """Comma-separated patterns are split."""
        result = normalize_file_patterns(
            [".github/dependabot.yml, .github/workflows/*"]
        )
        assert result == [".github/dependabot.yml", ".github/workflows/*"]

    def test_deduplication(self) -> None:
        """Duplicate patterns are removed."""
        result = normalize_file_patterns(["a.txt, b.txt, a.txt"])
        assert result == ["a.txt", "b.txt"]

    def test_strips_whitespace(self) -> None:
        """Whitespace is stripped."""
        result = normalize_file_patterns(["  a.txt  ,  b.txt  "])
        assert result == ["a.txt", "b.txt"]

    def test_drops_empties(self) -> None:
        """Empty entries are dropped."""
        result = normalize_file_patterns(["", "  ", ",,,"])
        assert result == []

    def test_empty_input(self) -> None:
        """Empty input returns empty list."""
        result = normalize_file_patterns([])
        assert result == []

    def test_preserves_regex_patterns(self) -> None:
        """Regex-prefixed patterns are preserved."""
        result = normalize_file_patterns([r"regex:\.pyc$"])
        assert result == [r"regex:\.pyc$"]

    def test_multiple_entries(self) -> None:
        """Multiple list entries are flattened."""
        result = normalize_file_patterns([".github/**", "*.bak, *.tmp"])
        assert result == [".github/**", "*.bak", "*.tmp"]


# ---------------------------------------------------------------------------
# _generate_replacement_string tests
# ---------------------------------------------------------------------------


class TestGenerateReplacementString:
    """Tests for the token replacement string generator."""

    def test_deterministic(self) -> None:
        """Same input produces same output."""
        a = _generate_replacement_string("secret-token-123")
        b = _generate_replacement_string("secret-token-123")
        assert a == b

    def test_different_inputs_different_outputs(self) -> None:
        """Different inputs produce different outputs."""
        a = _generate_replacement_string("token-a")
        b = _generate_replacement_string("token-b")
        assert a != b

    def test_prefix(self) -> None:
        """Output starts with REDACTED_ prefix."""
        result = _generate_replacement_string("any-token")
        assert result.startswith("REDACTED_")

    def test_different_length(self) -> None:
        """Output length differs from typical tokens."""
        token = "fake-test-token-abcdefghij1234"
        result = _generate_replacement_string(token)
        # REDACTED_ (9) + 12 hex chars = 21 chars
        assert len(result) == 21
        assert len(result) != len(token)


# ---------------------------------------------------------------------------
# parse_git_filter_spec tests
# ---------------------------------------------------------------------------


class TestParseGitFilterSpec:
    """Tests for the git filter spec parser."""

    def test_single_project_single_token(self) -> None:
        """Single project with single token."""
        result = parse_git_filter_spec("testsuite/pythonsdk-tests:glpat-abc123")
        assert result == {"testsuite/pythonsdk-tests": ["glpat-abc123"]}

    def test_single_project_multiple_tokens(self) -> None:
        """Single project with multiple tokens."""
        result = parse_git_filter_spec("my/project:token1,token2,token3")
        assert result == {"my/project": ["token1", "token2", "token3"]}

    def test_multiple_projects(self) -> None:
        """Multiple projects separated by semicolons."""
        result = parse_git_filter_spec("proj-a:tok1;proj-b:tok2,tok3")
        assert result == {
            "proj-a": ["tok1"],
            "proj-b": ["tok2", "tok3"],
        }

    def test_wildcard_project(self) -> None:
        """Wildcard project pattern."""
        result = parse_git_filter_spec("testsuite/*:leaked-secret")
        assert result == {"testsuite/*": ["leaked-secret"]}

    def test_empty_input(self) -> None:
        """Empty input returns empty dict."""
        assert parse_git_filter_spec("") == {}
        assert parse_git_filter_spec("  ") == {}

    def test_strips_whitespace(self) -> None:
        """Whitespace is stripped."""
        result = parse_git_filter_spec(" proj : tok1 , tok2 ; proj2 : tok3 ")
        assert result == {
            "proj": ["tok1", "tok2"],
            "proj2": ["tok3"],
        }

    def test_no_colon_warning(self) -> None:
        """Entry without colon is skipped with warning."""
        result = parse_git_filter_spec("invalid-entry")
        assert result == {}


# ---------------------------------------------------------------------------
# Integration tests using real git repos (require git)
# ---------------------------------------------------------------------------


class TestRemoveFilesFromBareRepo:
    """Integration tests for file removal from bare repos."""

    @pytest.fixture()
    def bare_repo_with_files(self, tmp_path: Path) -> Path:
        """Create a bare repo with some test files."""
        # Create a regular repo first
        regular = tmp_path / "regular"
        regular.mkdir()
        subprocess.run(
            ["git", "init", str(regular)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(regular), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(regular), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )

        # Create files
        (regular / "README.md").write_text("# Test\n")
        github_dir = regular / ".github"
        github_dir.mkdir()
        (github_dir / "dependabot.yml").write_text("version: 2\n")
        workflows_dir = github_dir / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "ci.yml").write_text("name: CI\non: push\n")
        src_dir = regular / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("print('hello')\n")

        # Commit
        subprocess.run(
            ["git", "-C", str(regular), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(regular), "commit", "-m", "Initial commit"],
            capture_output=True,
            check=True,
        )

        # Create bare clone
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "clone", "--mirror", str(regular), str(bare)],
            capture_output=True,
            check=True,
        )

        return bare

    def test_remove_no_patterns(self, bare_repo_with_files: Path) -> None:
        """No patterns means no removal."""
        removed = remove_files_from_bare_repo(bare_repo_with_files, [])
        assert removed == []

    def test_remove_nonexistent_repo(self, tmp_path: Path) -> None:
        """Nonexistent repo path returns empty."""
        removed = remove_files_from_bare_repo(tmp_path / "nonexistent", [".github/**"])
        assert removed == []

    @patch(
        "gerrit_clone.content_filter._check_git_filter_repo",
        return_value=False,
    )
    def test_worktree_removal(
        self,
        _mock_check: object,
        bare_repo_with_files: Path,
    ) -> None:
        """Worktree fallback removes files from branch tips."""
        # Detect the default branch name (may be master or main)
        branch_result = subprocess.run(
            [
                "git",
                "-C",
                str(bare_repo_with_files),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        branches = branch_result.stdout.strip().splitlines()
        assert len(branches) > 0, "Bare repo should have at least one branch"
        default_branch = branches[0]

        removed = remove_files_from_bare_repo(
            bare_repo_with_files,
            [".github/dependabot.yml"],
        )
        assert len(removed) > 0

        # Verify file is gone from branch tip
        result = subprocess.run(
            [
                "git",
                "-C",
                str(bare_repo_with_files),
                "ls-tree",
                "-r",
                "--name-only",
                default_branch,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        files = result.stdout.strip().splitlines()
        assert ".github/dependabot.yml" not in files
        assert "README.md" in files


class TestReplaceTokensInHistory:
    """Integration tests for token replacement."""

    @pytest.fixture()
    def repo_with_token(self, tmp_path: Path) -> Path:
        """Create a repo with a file containing a token."""
        repo = tmp_path / "token-repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", str(repo)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )

        # Create file with token (fake token to avoid push protection)
        config = repo / "config.py"
        config.write_text('TOKEN = "fake-test-token-abcdefghij1234"\n')
        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Add config with token"],
            capture_output=True,
            check=True,
        )

        # Remove the token in a second commit
        config.write_text('TOKEN = ""\n')
        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Remove token"],
            capture_output=True,
            check=True,
        )

        return repo

    def test_replace_requires_filter_repo(self, tmp_path: Path) -> None:
        """Raises RuntimeError when git-filter-repo is not available."""
        with (
            patch(
                "gerrit_clone.content_filter._check_git_filter_repo",
                return_value=False,
            ),
            pytest.raises(RuntimeError, match="git filter-repo"),
        ):
            replace_tokens_in_history(tmp_path, ["some-token"])

    def test_empty_tokens_succeeds(self, tmp_path: Path) -> None:
        """Empty token list returns True without doing anything."""
        assert replace_tokens_in_history(tmp_path, []) is True

    def test_successful_token_replacement(self, repo_with_token: Path) -> None:
        """Token is removed from all history when filter-repo is available."""
        if not shutil.which("git-filter-repo"):
            pytest.skip("git-filter-repo not installed")

        token = "fake-test-token-abcdefghij1234"
        result = replace_tokens_in_history(repo_with_token, [token])
        assert result is True

        # Verify the token no longer appears in any commit content
        log_result = subprocess.run(
            ["git", "-C", str(repo_with_token), "log", "--all", "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert token not in log_result.stdout
        assert "REDACTED_" in log_result.stdout


class TestRemoveFilesFilterRepo:
    """Unit tests for the git filter-repo code path."""

    @patch("gerrit_clone.content_filter._check_git_filter_repo", return_value=True)
    @patch("gerrit_clone.content_filter.subprocess.run")
    def test_glob_pattern_builds_path_glob_flag(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Glob patterns produce --path-glob --invert-paths args."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        repo = tmp_path / "test.git"
        repo.mkdir()

        result = _remove_files_filter_repo(repo, ["*.pyc"])

        assert result == ["*.pyc"]
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--path-glob" in cmd
        assert "*.pyc" in cmd
        assert "--invert-paths" in cmd

    @patch("gerrit_clone.content_filter._check_git_filter_repo", return_value=True)
    @patch("gerrit_clone.content_filter.subprocess.run")
    def test_regex_pattern_builds_path_regex_flag(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Regex patterns produce --path-regex --invert-paths args."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        repo = tmp_path / "test.git"
        repo.mkdir()

        result = _remove_files_filter_repo(repo, [r"regex:\.pyc$"])

        assert result == [r"regex:\.pyc$"]
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--path-regex" in cmd
        assert r"\.pyc$" in cmd
        assert "--invert-paths" in cmd

    @patch("gerrit_clone.content_filter._check_git_filter_repo", return_value=True)
    @patch("gerrit_clone.content_filter.subprocess.run")
    def test_exact_path_builds_path_flag(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Exact path patterns produce --path --invert-paths args."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        repo = tmp_path / "test.git"
        repo.mkdir()

        result = _remove_files_filter_repo(
            repo,
            [".github/dependabot.yml"],
        )

        assert result == [".github/dependabot.yml"]
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--path" in cmd
        idx = cmd.index("--path")
        assert cmd[idx + 1] == ".github/dependabot.yml"
        assert "--invert-paths" in cmd

    @patch("gerrit_clone.content_filter._check_git_filter_repo", return_value=True)
    @patch("gerrit_clone.content_filter.subprocess.run")
    def test_mixed_patterns_combined_in_single_command(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Multiple pattern types are combined into one command."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        repo = tmp_path / "test.git"
        repo.mkdir()

        patterns = ["exact.txt", "*.log", r"regex:\.bak$"]
        result = _remove_files_filter_repo(repo, patterns)

        assert result == patterns
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--path" in cmd
        assert "--path-glob" in cmd
        assert "--path-regex" in cmd

    @patch("gerrit_clone.content_filter._check_git_filter_repo", return_value=True)
    @patch("gerrit_clone.content_filter.subprocess.run")
    def test_failure_raises_runtime_error(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Non-zero exit from filter-repo raises RuntimeError."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="fatal: error",
            stdout="",
        )
        repo = tmp_path / "test.git"
        repo.mkdir()

        with pytest.raises(RuntimeError, match="git filter-repo failed"):
            _remove_files_filter_repo(repo, ["*.pyc"])


class TestApplyContentFilters:
    """Tests for the high-level apply_content_filters function."""

    def test_no_filters_succeeds(self, tmp_path: Path) -> None:
        """No filters applied returns success."""
        success, error = apply_content_filters(tmp_path, "test/project")
        assert success is True
        assert error is None

    def test_project_pattern_matching(self) -> None:
        """Git filter projects uses project pattern matching."""
        git_filters: dict[str, list[str]] = {
            "testsuite/*": ["token123"],
        }
        # Mock the actual filtering to just verify matching
        with patch(
            "gerrit_clone.content_filter.replace_tokens_in_history",
            return_value=True,
        ) as mock_replace:
            success, error = apply_content_filters(
                Path("/fake"),
                "testsuite/pythonsdk-tests",
                git_filter_projects=git_filters,
            )
            assert success is True
            assert error is None
            mock_replace.assert_called_once()

    def test_project_no_match_skips_filter(self) -> None:
        """Non-matching project skips token replacement."""
        git_filters: dict[str, list[str]] = {
            "testsuite/*": ["token123"],
        }
        with patch(
            "gerrit_clone.content_filter.replace_tokens_in_history",
        ) as mock_replace:
            success, _error = apply_content_filters(
                Path("/fake"),
                "oom/kubernetes",
                git_filter_projects=git_filters,
            )
            assert success is True
            mock_replace.assert_not_called()
