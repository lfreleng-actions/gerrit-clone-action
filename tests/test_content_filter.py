# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for content filtering: file removal and token replacement."""

from __future__ import annotations

import random
import re as re_mod
import shutil
import string
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gerrit_clone.content_filter import (
    SECRET_PATTERNS,
    _generate_replacement_string,
    _remove_files_filter_repo,
    apply_content_filters,
    match_file_pattern,
    normalize_file_patterns,
    parse_git_filter_spec,
    remove_files_from_bare_repo,
    replace_tokens_in_history,
    scan_repo_for_secrets,
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


# ---------------------------------------------------------------------------
# Runtime token generation for tests
# ---------------------------------------------------------------------------
#
# Tokens are built dynamically so that no real credential literals
# appear in the source file.  This avoids triggering GitHub push
# protection while still exercising the actual regex patterns.
# ---------------------------------------------------------------------------


def _make_fake_token(
    prefix: str,
    suffix_len: int,
    *,
    chars: str | None = None,
) -> str:
    """Build a deterministic fake credential string at runtime.

    Args:
        prefix: Fixed prefix for the token (e.g. ``"glpat-"``).
        suffix_len: Number of random characters after the prefix.
        chars: Character pool for the suffix.  Defaults to
            ASCII letters + digits.

    Returns:
        A string like ``prefix + <suffix_len random chars>``.
    """
    pool = chars or (string.ascii_letters + string.digits)
    rng = random.Random(f"test-seed-{prefix}-{suffix_len}")
    return prefix + "".join(rng.choices(pool, k=suffix_len))


#: Pre-built fake tokens keyed by SECRET_PATTERNS name.
#: Every value is guaranteed to match the corresponding pattern.
_TEST_TOKENS: dict[str, str] = {
    "gitlab_pat": _make_fake_token(
        "glpat-", 22, chars=string.ascii_letters + string.digits + "_-"
    ),
    "github_pat_classic": _make_fake_token("ghp_", 36),
    "github_pat_fine_grained": _make_fake_token(
        "github_pat_", 30, chars=string.ascii_letters + string.digits + "_"
    ),
    "github_oauth": _make_fake_token("gho_", 36),
    "github_app_user": _make_fake_token("ghu_", 36),
    "github_app_server": _make_fake_token("ghs_", 36),
    "github_app_refresh": _make_fake_token("ghr_", 36),
    "aws_access_key_id": _make_fake_token(
        "AKIA", 16, chars=string.ascii_uppercase + string.digits
    ),
    "slack_token": _make_fake_token(
        "xoxb-", 40, chars=string.ascii_letters + string.digits + "-"
    ),
    "stripe_api_key": _make_fake_token(
        "sk_test_", 24, chars=string.ascii_letters + string.digits
    ),
    "sendgrid_api_key": (
        "SG."
        + _make_fake_token("", 22, chars=string.ascii_letters + string.digits + "_-")
        + "."
        + _make_fake_token("", 22, chars=string.ascii_letters + string.digits + "_-")
    ),
    "google_api_key": _make_fake_token(
        "AIza", 35, chars=string.ascii_letters + string.digits + "_-"
    ),
    "npm_token": _make_fake_token("npm_", 36),
    "pypi_token": _make_fake_token(
        "pypi-", 55, chars=string.ascii_letters + string.digits + "_-"
    ),
}


# ---------------------------------------------------------------------------
# SECRET_PATTERNS tests
# ---------------------------------------------------------------------------


class TestSecretPatterns:
    """Tests for the built-in credential pattern library."""

    @pytest.mark.parametrize(
        ("pattern_name", "sample"),
        list(_TEST_TOKENS.items()),
    )
    def test_pattern_matches_sample(self, pattern_name: str, sample: str) -> None:
        """Each dynamically generated token matches its pattern."""
        pattern = SECRET_PATTERNS[pattern_name]
        assert pattern.search(sample), (
            f"Pattern '{pattern_name}' did not match: {sample}"
        )

    @pytest.mark.parametrize(
        ("pattern_name", "non_match"),
        [
            ("gitlab_pat", "not-a-token"),
            ("gitlab_pat", "glpat-short"),
            ("github_pat_classic", "ghp_tooshort"),
            ("aws_access_key_id", "AKIA1234"),
            ("slack_token", "xoxb-short"),
        ],
    )
    def test_pattern_rejects_non_match(self, pattern_name: str, non_match: str) -> None:
        """Patterns do not match non-credential strings."""
        pattern = SECRET_PATTERNS[pattern_name]
        assert not pattern.search(non_match), (
            f"Pattern '{pattern_name}' should not match: {non_match}"
        )

    def test_all_patterns_are_compiled(self) -> None:
        """All entries in SECRET_PATTERNS are compiled regexes."""
        for name, pat in SECRET_PATTERNS.items():
            assert isinstance(pat, re_mod.Pattern), f"{name} is not a compiled pattern"


# ---------------------------------------------------------------------------
# scan_repo_for_secrets tests
# ---------------------------------------------------------------------------

# Use the dynamically generated GitLab PAT for repo fixtures
_FAKE_GITLAB_PAT = _TEST_TOKENS["gitlab_pat"]


class TestScanRepoForSecrets:
    """Tests for automatic secret scanning."""

    @pytest.fixture()
    def repo_with_gitlab_pat(self, tmp_path: Path) -> Path:
        """Create a repo with a fake GitLab PAT in history."""
        repo = tmp_path / "secret-repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", str(repo)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "user.email",
                "test@test.com",
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "user.name",
                "Test",
            ],
            capture_output=True,
            check=True,
        )

        # Commit a file containing a dynamically generated GitLab PAT
        config_file = repo / "settings.py"
        config_file.write_text(f'GITLAB_TOKEN = "{_FAKE_GITLAB_PAT}"\n')
        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Add settings"],
            capture_output=True,
            check=True,
        )

        # Remove the token in a second commit
        config_file.write_text('GITLAB_TOKEN = ""\n')
        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "commit",
                "-m",
                "Remove token",
            ],
            capture_output=True,
            check=True,
        )

        return repo

    def test_discovers_gitlab_pat(self, repo_with_gitlab_pat: Path) -> None:
        """Scan finds a GitLab PAT in repository history."""
        found = scan_repo_for_secrets(repo_with_gitlab_pat)
        assert len(found) >= 1
        assert _FAKE_GITLAB_PAT in found

    def test_returns_empty_for_clean_repo(self, tmp_path: Path) -> None:
        """Scan returns empty list for repo without secrets."""
        repo = tmp_path / "clean-repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", str(repo)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "user.email",
                "test@test.com",
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "user.name",
                "Test",
            ],
            capture_output=True,
            check=True,
        )
        readme = repo / "README.md"
        readme.write_text("# Clean project\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Init"],
            capture_output=True,
            check=True,
        )

        found = scan_repo_for_secrets(repo)
        assert found == []

    def test_returns_empty_for_nonexistent_path(self, tmp_path: Path) -> None:
        """Scan returns empty list for nonexistent repo path."""
        found = scan_repo_for_secrets(tmp_path / "does-not-exist")
        assert found == []

    def test_deduplicates_tokens(self, repo_with_gitlab_pat: Path) -> None:
        """Same token appearing multiple times is deduplicated."""
        found = scan_repo_for_secrets(repo_with_gitlab_pat)
        # The token appears in add and delete commits but
        # should only appear once in the results
        token_count = found.count(_FAKE_GITLAB_PAT)
        assert token_count == 1


# ---------------------------------------------------------------------------
# apply_content_filters with redact_secrets tests
# ---------------------------------------------------------------------------


class TestApplyContentFiltersRedactSecrets:
    """Tests for apply_content_filters with redact_secrets=True."""

    def test_redact_secrets_calls_scan(self) -> None:
        """When redact_secrets=True, scan_repo_for_secrets is called."""
        with (
            patch(
                "gerrit_clone.content_filter.scan_repo_for_secrets",
                return_value=["fake-token-123"],
            ) as mock_scan,
            patch(
                "gerrit_clone.content_filter.replace_tokens_in_history",
                return_value=True,
            ) as mock_replace,
        ):
            success, error = apply_content_filters(
                Path("/fake/repo"),
                "test/project",
                redact_secrets=True,
            )
            assert success is True
            assert error is None
            mock_scan.assert_called_once()
            mock_replace.assert_called_once_with(
                Path("/fake/repo"),
                ["fake-token-123"],
                timeout=600,
            )

    def test_redact_secrets_no_findings(self) -> None:
        """No error when scan finds nothing."""
        with patch(
            "gerrit_clone.content_filter.scan_repo_for_secrets",
            return_value=[],
        ) as mock_scan:
            success, error = apply_content_filters(
                Path("/fake/repo"),
                "test/project",
                redact_secrets=True,
            )
            assert success is True
            assert error is None
            mock_scan.assert_called_once()

    def test_redact_secrets_false_skips_scan(self) -> None:
        """When redact_secrets=False (default), no scan runs."""
        with patch(
            "gerrit_clone.content_filter.scan_repo_for_secrets",
        ) as mock_scan:
            success, error = apply_content_filters(
                Path("/fake/repo"),
                "test/project",
                redact_secrets=False,
            )
            assert success is True
            assert error is None
            mock_scan.assert_not_called()

    def test_redact_secrets_combined_with_git_filter(
        self,
    ) -> None:
        """Explicit git-filter and redact-secrets can run together."""
        git_filters: dict[str, list[str]] = {
            "test/*": ["explicit-token"],
        }
        with (
            patch(
                "gerrit_clone.content_filter.replace_tokens_in_history",
                return_value=True,
            ) as mock_replace,
            patch(
                "gerrit_clone.content_filter.scan_repo_for_secrets",
                return_value=["auto-discovered-token"],
            ),
        ):
            success, error = apply_content_filters(
                Path("/fake/repo"),
                "test/project",
                git_filter_projects=git_filters,
                redact_secrets=True,
            )
            assert success is True
            assert error is None
            # Should be called twice: once for explicit, once for auto
            assert mock_replace.call_count == 2

    def test_redact_secrets_failure_reports_error(self) -> None:
        """Failure during auto-redaction is reported."""
        with (
            patch(
                "gerrit_clone.content_filter.scan_repo_for_secrets",
                return_value=["leaked-token"],
            ),
            patch(
                "gerrit_clone.content_filter.replace_tokens_in_history",
                return_value=False,
            ),
        ):
            success, error = apply_content_filters(
                Path("/fake/repo"),
                "test/project",
                redact_secrets=True,
            )
            assert success is False
            assert error is not None
            assert "Auto-redaction failed" in error
