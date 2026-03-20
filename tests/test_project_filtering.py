# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for project filtering: wildcards, include/exclude, normalization."""

from __future__ import annotations

from typing import ClassVar

from gerrit_clone.mirror_manager import filter_projects_by_hierarchy
from gerrit_clone.models import (
    Config,
    Project,
    ProjectState,
    filter_projects,
    match_project_pattern,
    normalize_project_list,
)

# ---------------------------------------------------------------------------
# Helper to quickly build Project objects
# ---------------------------------------------------------------------------


def _p(name: str, state: ProjectState = ProjectState.ACTIVE) -> Project:
    """Shorthand factory for test Project instances."""
    return Project(name=name, state=state)


# ---------------------------------------------------------------------------
# match_project_pattern
# ---------------------------------------------------------------------------


class TestMatchProjectPattern:
    """Tests for the match_project_pattern utility."""

    # -- exact match --------------------------------------------------------

    def test_exact_match(self) -> None:
        assert match_project_pattern("ccsdk", "ccsdk") is True

    def test_exact_match_with_slashes(self) -> None:
        assert (
            match_project_pattern(
                "testsuite/pythonsdk-tests", "testsuite/pythonsdk-tests"
            )
            is True
        )

    def test_no_match_different_name(self) -> None:
        assert match_project_pattern("oom", "ccsdk") is False

    # -- hierarchical (plain pattern, no wildcards) -------------------------

    def test_hierarchical_child_match(self) -> None:
        assert match_project_pattern("ccsdk/apps", "ccsdk") is True

    def test_hierarchical_grandchild_match(self) -> None:
        assert match_project_pattern("ccsdk/features/test", "ccsdk") is True

    def test_hierarchical_no_partial_prefix(self) -> None:
        """'ccsdkfoo' must NOT match pattern 'ccsdk' (no slash separator)."""
        assert match_project_pattern("ccsdkfoo", "ccsdk") is False

    def test_hierarchical_parent_does_not_match_child_pattern(self) -> None:
        """Pattern 'ccsdk/apps' should NOT match project 'ccsdk'."""
        assert match_project_pattern("ccsdk", "ccsdk/apps") is False

    # -- wildcard: star (*) -------------------------------------------------

    def test_wildcard_star_prefix(self) -> None:
        assert match_project_pattern("pythonsdk-tests", "*sdk*") is True

    def test_wildcard_star_suffix(self) -> None:
        assert match_project_pattern("ccsdk", "cc*") is True

    def test_wildcard_star_middle(self) -> None:
        assert match_project_pattern("testsuite/pythonsdk-tests", "*pythonsdk*") is True

    def test_wildcard_star_no_match(self) -> None:
        assert match_project_pattern("oom", "*sdk*") is False

    def test_wildcard_match_all(self) -> None:
        assert match_project_pattern("anything/at/all", "*") is True

    # -- wildcard: question mark (?) ----------------------------------------

    def test_wildcard_question_mark(self) -> None:
        assert match_project_pattern("oom", "oo?") is True

    def test_wildcard_question_mark_no_match(self) -> None:
        assert match_project_pattern("oom", "o?") is False

    # -- wildcard: character class ([seq]) ----------------------------------

    def test_wildcard_char_class(self) -> None:
        assert match_project_pattern("oom", "[o]om") is True

    def test_wildcard_negated_char_class(self) -> None:
        assert match_project_pattern("oom", "[!a]om") is True
        assert match_project_pattern("oom", "[!o]om") is False

    # -- case sensitivity ---------------------------------------------------

    def test_case_sensitive(self) -> None:
        """Matching must be case-sensitive."""
        assert match_project_pattern("CCSDK", "ccsdk") is False
        assert match_project_pattern("ccsdk", "CCSDK") is False

    # -- edge cases ---------------------------------------------------------

    def test_empty_project_name(self) -> None:
        assert match_project_pattern("", "ccsdk") is False

    def test_empty_pattern(self) -> None:
        assert match_project_pattern("ccsdk", "") is False

    def test_both_empty(self) -> None:
        assert match_project_pattern("", "") is True

    def test_slash_only_pattern(self) -> None:
        assert match_project_pattern("a/b", "/") is False

    def test_pattern_with_trailing_slash(self) -> None:
        """Trailing slash in pattern should not accidentally match."""
        assert match_project_pattern("ccsdk/apps", "ccsdk/") is False
        # But with a wildcard it would:
        assert match_project_pattern("ccsdk/apps", "ccsdk/*") is True


# ---------------------------------------------------------------------------
# normalize_project_list
# ---------------------------------------------------------------------------


class TestNormalizeProjectList:
    """Tests for the normalize_project_list helper."""

    def test_comma_separated(self) -> None:
        result = normalize_project_list(["ccsdk,oom,cps"])
        assert result == ["ccsdk", "oom", "cps"]

    def test_space_separated(self) -> None:
        result = normalize_project_list(["ccsdk oom cps"])
        assert result == ["ccsdk", "oom", "cps"]

    def test_comma_and_space_mixed(self) -> None:
        result = normalize_project_list(["ccsdk, oom cps"])
        assert result == ["ccsdk", "oom", "cps"]

    def test_multiple_entries(self) -> None:
        result = normalize_project_list(["ccsdk,oom", "cps", "aai"])
        assert result == ["ccsdk", "oom", "cps", "aai"]

    def test_deduplication_preserves_order(self) -> None:
        result = normalize_project_list(["ccsdk,oom,ccsdk,oom"])
        assert result == ["ccsdk", "oom"]

    def test_strips_whitespace(self) -> None:
        result = normalize_project_list(["  ccsdk  ,  oom  "])
        assert result == ["ccsdk", "oom"]

    def test_drops_empties(self) -> None:
        result = normalize_project_list(["", "  ", ",,,"])
        assert result == []

    def test_empty_input(self) -> None:
        result = normalize_project_list([])
        assert result == []

    def test_wildcards_preserved(self) -> None:
        result = normalize_project_list(["*sdk*, testsuite/*"])
        assert result == ["*sdk*", "testsuite/*"]


# ---------------------------------------------------------------------------
# filter_projects
# ---------------------------------------------------------------------------


class TestFilterProjects:
    """Tests for the filter_projects function."""

    PROJECTS: ClassVar[list[Project]] = [
        _p("ccsdk"),
        _p("ccsdk/apps"),
        _p("ccsdk/features"),
        _p("ccsdk/features/test"),
        _p("oom"),
        _p("oom/kubernetes"),
        _p("cps"),
        _p("testsuite"),
        _p("testsuite/pythonsdk-tests"),
        _p("aai"),
    ]

    # -- include only -------------------------------------------------------

    def test_include_exact(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=["oom"])
        names = {p.name for p in result}
        assert names == {"oom", "oom/kubernetes"}

    def test_include_hierarchy(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=["ccsdk"])
        names = {p.name for p in result}
        assert names == {"ccsdk", "ccsdk/apps", "ccsdk/features", "ccsdk/features/test"}

    def test_include_multiple(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=["ccsdk", "oom"])
        names = {p.name for p in result}
        assert "ccsdk" in names
        assert "ccsdk/apps" in names
        assert "oom" in names
        assert "oom/kubernetes" in names
        assert "cps" not in names

    def test_include_wildcard(self) -> None:
        """Wildcard *sdk* matches any project whose full name contains 'sdk'
        via fnmatch, for example 'ccsdk' and 'testsuite/pythonsdk-tests'.
        Hierarchical expansion is not applied for wildcard patterns; each
        project name is matched against '*sdk*' independently."""
        result = filter_projects(self.PROJECTS, include_patterns=["*sdk*"])
        names = {p.name for p in result}
        # fnmatch matches any name containing "sdk" anywhere in the full string
        assert (
            names
            == {
                "ccsdk",
                "ccsdk/apps",  # not via hierarchy — via fnmatch (contains "sdk" in "ccsdk")
                "ccsdk/features",  # not via hierarchy — via fnmatch (contains "sdk" in "ccsdk")
                "ccsdk/features/test",  # not via hierarchy — via fnmatch (contains "sdk" in "ccsdk")
                "testsuite/pythonsdk-tests",  # contains "sdk"
            }
        )
        # All ccsdk/* names above match '*sdk*' because their full paths contain
        # the substring "sdk" (in the leading "ccsdk" component).

    def test_include_no_match_returns_empty(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=["nonexistent"])
        assert result == []

    def test_include_none_returns_all(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=None)
        assert len(result) == len(self.PROJECTS)

    def test_include_empty_list_returns_all(self) -> None:
        result = filter_projects(self.PROJECTS, include_patterns=[])
        assert len(result) == len(self.PROJECTS)

    # -- exclude only -------------------------------------------------------

    def test_exclude_exact(self) -> None:
        result = filter_projects(
            self.PROJECTS,
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        assert "testsuite/pythonsdk-tests" not in names
        # Parent should still be present
        assert "testsuite" in names
        assert len(result) == len(self.PROJECTS) - 1

    def test_exclude_hierarchy(self) -> None:
        """Excluding 'ccsdk' removes ccsdk and all its children."""
        result = filter_projects(self.PROJECTS, exclude_patterns=["ccsdk"])
        names = {p.name for p in result}
        assert "ccsdk" not in names
        assert "ccsdk/apps" not in names
        assert "ccsdk/features" not in names
        assert "ccsdk/features/test" not in names
        assert "oom" in names

    def test_exclude_wildcard(self) -> None:
        result = filter_projects(self.PROJECTS, exclude_patterns=["*test*"])
        names = {p.name for p in result}
        assert "ccsdk/features/test" not in names
        assert "testsuite/pythonsdk-tests" not in names
        # "testsuite" itself is also matched by "*test*" because it contains
        # the substring "test" and fnmatch patterns are anchored to the
        # whole string but allow "*" to match any prefix/suffix. Verify:
        assert "testsuite" not in names

    def test_exclude_none_returns_all(self) -> None:
        result = filter_projects(self.PROJECTS, exclude_patterns=None)
        assert len(result) == len(self.PROJECTS)

    def test_exclude_empty_list_returns_all(self) -> None:
        result = filter_projects(self.PROJECTS, exclude_patterns=[])
        assert len(result) == len(self.PROJECTS)

    # -- include + exclude combined -----------------------------------------

    def test_include_then_exclude(self) -> None:
        """Include 'testsuite' hierarchy, then exclude the problematic repo."""
        result = filter_projects(
            self.PROJECTS,
            include_patterns=["testsuite"],
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        assert names == {"testsuite"}

    def test_include_all_exclude_one(self) -> None:
        """No include filter (everything), exclude one specific repo."""
        result = filter_projects(
            self.PROJECTS,
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        assert len(result) == len(self.PROJECTS) - 1
        assert all(p.name != "testsuite/pythonsdk-tests" for p in result)

    def test_include_wildcard_exclude_specific(self) -> None:
        """Include all *sdk* projects, but exclude the problematic one."""
        result = filter_projects(
            self.PROJECTS,
            include_patterns=["*sdk*"],
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        # *sdk* matches every name that contains "sdk" (fnmatch), which
        # includes ccsdk, ccsdk/apps, ccsdk/features, ccsdk/features/test,
        # and testsuite/pythonsdk-tests.  The exclude then removes the
        # problematic repo.
        assert names == {
            "ccsdk",
            "ccsdk/apps",
            "ccsdk/features",
            "ccsdk/features/test",
        }

    def test_exclude_overrides_include(self) -> None:
        """If something matches both include and exclude, exclude wins."""
        result = filter_projects(
            self.PROJECTS,
            include_patterns=["ccsdk"],
            exclude_patterns=["ccsdk/features"],
        )
        names = {p.name for p in result}
        assert "ccsdk" in names
        assert "ccsdk/apps" in names
        # ccsdk/features and its children should be excluded
        assert "ccsdk/features" not in names
        assert "ccsdk/features/test" not in names

    # -- ordering preserved -------------------------------------------------

    def test_order_preserved(self) -> None:
        """Filtering must preserve the original order of projects."""
        result = filter_projects(
            self.PROJECTS,
            include_patterns=["oom", "cps"],
        )
        names = [p.name for p in result]
        assert names == ["oom", "oom/kubernetes", "cps"]

    # -- empty input --------------------------------------------------------

    def test_empty_projects_list(self) -> None:
        result = filter_projects([], include_patterns=["ccsdk"])
        assert result == []

    def test_empty_projects_with_exclude(self) -> None:
        result = filter_projects([], exclude_patterns=["ccsdk"])
        assert result == []

    # -- does not mutate original -------------------------------------------

    def test_original_not_mutated(self) -> None:
        original = list(self.PROJECTS)
        filter_projects(
            self.PROJECTS,
            include_patterns=["ccsdk"],
            exclude_patterns=["ccsdk/apps"],
        )
        assert original == self.PROJECTS


# ---------------------------------------------------------------------------
# Config normalization of include_projects / exclude_projects
# ---------------------------------------------------------------------------


class TestConfigProjectNormalization:
    """Tests for Config.__post_init__ normalization of project lists."""

    def _make_config(self, **kwargs: object) -> Config:
        """Build a Config with sensible defaults, overriding with kwargs."""
        defaults: dict[str, object] = {
            "host": "gerrit.example.org",
        }
        defaults.update(kwargs)
        return Config(**defaults)  # type: ignore[arg-type]

    def test_include_comma_separated(self) -> None:
        cfg = self._make_config(include_projects=["ccsdk,oom,cps"])
        assert cfg.include_projects == ["ccsdk", "oom", "cps"]

    def test_include_space_separated(self) -> None:
        cfg = self._make_config(include_projects=["ccsdk oom cps"])
        assert cfg.include_projects == ["ccsdk", "oom", "cps"]

    def test_include_dedup(self) -> None:
        cfg = self._make_config(include_projects=["ccsdk,oom,ccsdk"])
        assert cfg.include_projects == ["ccsdk", "oom"]

    def test_exclude_comma_separated(self) -> None:
        cfg = self._make_config(
            exclude_projects=["testsuite/pythonsdk-tests, other/bad-repo"]
        )
        assert cfg.exclude_projects == ["testsuite/pythonsdk-tests", "other/bad-repo"]

    def test_exclude_space_separated(self) -> None:
        cfg = self._make_config(exclude_projects=["repo-a repo-b"])
        assert cfg.exclude_projects == ["repo-a", "repo-b"]

    def test_exclude_dedup(self) -> None:
        cfg = self._make_config(exclude_projects=["a,b,a"])
        assert cfg.exclude_projects == ["a", "b"]

    def test_empty_include_stays_empty(self) -> None:
        cfg = self._make_config(include_projects=[])
        assert cfg.include_projects == []

    def test_empty_exclude_stays_empty(self) -> None:
        cfg = self._make_config(exclude_projects=[])
        assert cfg.exclude_projects == []

    def test_wildcards_preserved_in_config(self) -> None:
        cfg = self._make_config(
            include_projects=["*sdk*"],
            exclude_projects=["testsuite/*"],
        )
        assert cfg.include_projects == ["*sdk*"]
        assert cfg.exclude_projects == ["testsuite/*"]


# ---------------------------------------------------------------------------
# filter_projects_by_hierarchy (mirror_manager wrapper)
# ---------------------------------------------------------------------------


class TestFilterProjectsByHierarchy:
    """Tests for the mirror_manager.filter_projects_by_hierarchy wrapper."""

    PROJECTS: ClassVar[list[Project]] = [
        _p("ccsdk"),
        _p("ccsdk/apps"),
        _p("ccsdk/features"),
        _p("ccsdk/features/test"),
        _p("oom"),
        _p("oom/kubernetes"),
        _p("cps"),
        _p("testsuite"),
        _p("testsuite/pythonsdk-tests"),
    ]

    def test_empty_filters_returns_all(self) -> None:
        result = filter_projects_by_hierarchy(self.PROJECTS, [])
        assert len(result) == len(self.PROJECTS)

    def test_hierarchy_include(self) -> None:
        result = filter_projects_by_hierarchy(self.PROJECTS, ["ccsdk"])
        names = {p.name for p in result}
        assert names == {"ccsdk", "ccsdk/apps", "ccsdk/features", "ccsdk/features/test"}

    def test_hierarchy_exclude(self) -> None:
        result = filter_projects_by_hierarchy(
            self.PROJECTS,
            [],
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        assert "testsuite/pythonsdk-tests" not in names
        assert "testsuite" in names
        assert len(result) == len(self.PROJECTS) - 1

    def test_hierarchy_include_and_exclude(self) -> None:
        result = filter_projects_by_hierarchy(
            self.PROJECTS,
            ["ccsdk", "oom"],
            exclude_patterns=["ccsdk/features"],
        )
        names = {p.name for p in result}
        assert "ccsdk" in names
        assert "ccsdk/apps" in names
        assert "oom" in names
        assert "oom/kubernetes" in names
        # ccsdk/features and its children excluded
        assert "ccsdk/features" not in names
        assert "ccsdk/features/test" not in names

    def test_hierarchy_wildcard_include(self) -> None:
        result = filter_projects_by_hierarchy(self.PROJECTS, ["*sdk*"])
        names = {p.name for p in result}
        assert "ccsdk" in names
        assert "testsuite/pythonsdk-tests" in names

    def test_hierarchy_wildcard_exclude(self) -> None:
        result = filter_projects_by_hierarchy(
            self.PROJECTS,
            [],
            exclude_patterns=["*sdk*"],
        )
        names = {p.name for p in result}
        assert "ccsdk" not in names
        assert "testsuite/pythonsdk-tests" not in names
        assert "oom" in names
        assert "cps" in names

    def test_no_partial_name_match(self) -> None:
        """Regression: 'ccsdkfoo' must not match pattern 'ccsdk'."""
        projects = [_p("ccsdk"), _p("ccsdk/apps"), _p("ccsdkfoo")]
        result = filter_projects_by_hierarchy(projects, ["ccsdk"])
        names = {p.name for p in result}
        assert "ccsdkfoo" not in names
        assert "ccsdk" in names
        assert "ccsdk/apps" in names


# ---------------------------------------------------------------------------
# Real-world scenario: ONAP testsuite/pythonsdk-tests exclusion
# ---------------------------------------------------------------------------


class TestONAPExclusionScenario:
    """End-to-end scenario: mirror all ONAP repos except the one with a leaked credential."""

    ONAP_SAMPLE: ClassVar[list[Project]] = [
        _p("aai"),
        _p("aai/aai-common"),
        _p("ccsdk"),
        _p("ccsdk/apps"),
        _p("cps"),
        _p("oom"),
        _p("oom/kubernetes"),
        _p("testsuite"),
        _p("testsuite/pythonsdk-tests"),
        _p("testsuite/heatbridge"),
    ]

    def test_exclude_single_problematic_repo(self) -> None:
        result = filter_projects(
            self.ONAP_SAMPLE,
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        assert "testsuite/pythonsdk-tests" not in names
        assert "testsuite" in names
        assert "testsuite/heatbridge" in names
        assert len(result) == len(self.ONAP_SAMPLE) - 1

    def test_config_driven_exclusion(self) -> None:
        """Config with exclude_projects correctly filters via filter_projects."""
        cfg = Config(
            host="gerrit.onap.org",
            exclude_projects=["testsuite/pythonsdk-tests"],
        )
        result = filter_projects(
            self.ONAP_SAMPLE,
            include_patterns=cfg.include_projects or None,
            exclude_patterns=cfg.exclude_projects or None,
        )
        names = {p.name for p in result}
        assert "testsuite/pythonsdk-tests" not in names
        assert len(result) == len(self.ONAP_SAMPLE) - 1

    def test_mirror_hierarchy_with_exclusion(self) -> None:
        """Mirror-style filtering: include 'testsuite', exclude the problematic child."""
        result = filter_projects_by_hierarchy(
            self.ONAP_SAMPLE,
            ["testsuite"],
            exclude_patterns=["testsuite/pythonsdk-tests"],
        )
        names = {p.name for p in result}
        assert names == {"testsuite", "testsuite/heatbridge"}
