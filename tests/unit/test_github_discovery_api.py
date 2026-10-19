# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub discovery against a fake GitHub API.

Only :class:`httpx.Client` is replaced: the query text, pagination,
retries and fallbacks all run as they do against GitHub.  The fake
records every request, so tests can assert which host and endpoint
each one went to, as well as what discovery returned.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from source_clone.github_discovery import discover_github_repositories
from source_clone.github_models import GitHubAuthError, GitHubNotFoundError
from source_clone.models import Config, SourceType

if TYPE_CHECKING:
    from collections.abc import Iterator

GITHUB_GRAPHQL = "https://api.github.com/graphql"
GITHUB_REST = "https://api.github.com"
GHES_HOST = "github.example.com"
GHES_GRAPHQL = f"https://{GHES_HOST}/api/graphql"
GHES_REST = f"https://{GHES_HOST}/api/v3"


def _node(owner: str, name: str) -> dict[str, Any]:
    """A GraphQL repository node as GitHub returns it."""
    return {
        "name": name,
        "nameWithOwner": f"{owner}/{name}",
        "url": f"https://github.com/{owner}/{name}",
        "sshUrl": f"git@github.com:{owner}/{name}.git",
        "isPrivate": False,
        "isArchived": False,
        "description": None,
        "defaultBranchRef": {
            "name": "main",
            "target": {"oid": "a" * 40, "committedDate": "2026-01-01T00:00:00Z"},
        },
    }


def _rest_repo(owner: str, name: str) -> dict[str, Any]:
    """A REST repository object as GitHub returns it."""
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "html_url": f"https://github.com/{owner}/{name}",
        "clone_url": f"https://github.com/{owner}/{name}.git",
        "ssh_url": f"git@github.com:{owner}/{name}.git",
        "private": False,
        "archived": False,
    }


def _response(payload: Any, status: int = 200) -> Mock:
    """An httpx-like response carrying *payload* as JSON."""
    response = Mock()
    response.status_code = status
    response.headers = {}
    response.json.return_value = payload
    response.content = json.dumps(payload).encode()
    response.raise_for_status.return_value = None
    return response


class FakeGitHub:
    """Answers GraphQL and REST requests the way github.com does.

    Organisations and users each own a set of repositories.  The token
    owner is a user who also belongs to other owners' repositories, so
    ``/user/repos`` returns repositories that are not theirs.
    """

    def __init__(
        self,
        *,
        orgs: dict[str, list[str]] | None = None,
        users: dict[str, list[str]] | None = None,
        token_owner_repos: list[tuple[str, str]] | None = None,
    ) -> None:
        self.orgs = orgs or {}
        self.users = users or {}
        self.token_owner_repos = token_owner_repos or []
        self.posts: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str]] = []

    def _owner_payload(self, login: str) -> dict[str, Any] | None:
        """The repositoryOwner object for *login*, or None if unknown."""
        if login in self.orgs:
            typename, names = "Organization", self.orgs[login]
        elif login in self.users:
            typename, names = "User", self.users[login]
        else:
            return None
        return {
            "__typename": typename,
            "login": login,
            "repositories": {
                "nodes": [_node(login, n) for n in names],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }

    def post(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> Mock:
        """Answer a GraphQL query."""
        query: str = json["query"]
        login: str = (json.get("variables") or {}).get("login", "")
        self.posts.append((url, query))
        if "repositoryOwner" in query:
            return _response({"data": {"repositoryOwner": self._owner_payload(login)}})
        # Legacy organisation query: the login is inlined in the text.
        login = query.split('organization(login: "', 1)[1].split('"', 1)[0]
        if login in self.orgs:
            return _response({"data": {"organization": self._owner_payload(login)}})
        return _response(
            {
                "data": {"organization": None},
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "message": "Could not resolve to an Organization "
                        f"with the login of '{login}'.",
                    }
                ],
            }
        )

    def request(self, method: str, url: str, **_kwargs: Any) -> Mock:
        """Answer a REST request."""
        self.requests.append((method, url))
        parsed = urlsplit(url)
        path = parsed.path
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        if path.endswith("/user/repos"):
            repos = [_rest_repo(o, n) for o, n in self.token_owner_repos]
        elif "/users/" in path and path.endswith("/repos"):
            login = path.split("/users/", 1)[1].split("/", 1)[0]
            repos = [_rest_repo(login, n) for n in self.users.get(login, [])]
        else:
            return _response({"message": "Not Found"}, status=404)
        return _response(repos if page == 1 else [])


@pytest.fixture
def no_sleep() -> Iterator[Mock]:
    """Make GraphQL retry backoff instant, recording each pause."""
    with patch("source_clone.github_graphql_retry.time_mod.sleep") as sleep:
        yield sleep


def _discover(fake: FakeGitHub, host: str, **config: Any) -> list[str]:
    """Run discovery against *fake*; return the discovered names."""
    cfg = Config(
        host=host,
        source_type=SourceType.GITHUB,
        github_token="test-token",
        **config,
    )
    with (
        patch("httpx.Client.post", side_effect=fake.post),
        patch("httpx.Client.request", side_effect=fake.request),
    ):
        projects, _stats = discover_github_repositories(cfg)
    return sorted(p.name for p in projects)


def _hosts(urls: list[str]) -> set[str]:
    """The scheme and host of each URL."""
    return {"/".join(u.split("/", 3)[:3]) for u in urls}


class TestEnterpriseHost:
    """#322: discovery talks to the configured GitHub host only."""

    def test_enterprise_requests_go_to_enterprise(self) -> None:
        """Every request for an Enterprise host goes to that host."""
        fake = FakeGitHub(orgs={"engineering": ["svc"]})

        names = _discover(fake, f"{GHES_HOST}/engineering")

        assert names == ["svc"]
        urls = [u for u, _ in fake.posts] + [u for _, u in fake.requests]
        assert _hosts(urls) == {f"https://{GHES_HOST}"}
        assert {u for u, _ in fake.posts} == {GHES_GRAPHQL}

    def test_github_com_requests_go_to_github_com(self) -> None:
        """github.com discovery keeps using api.github.com."""
        fake = FakeGitHub(orgs={"testorg": ["repo1"]})

        names = _discover(fake, "github.com/testorg")

        assert names == ["repo1"]
        assert {u for u, _ in fake.posts} == {GITHUB_GRAPHQL}

    def test_gerrit_base_url_never_receives_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Gerrit API base URL in the environment isn't a GitHub API.

        ``GERRIT_BASE_URL`` and ``--base-url`` name the Gerrit REST
        API.  Using either as the GitHub API would send the GitHub
        token to the Gerrit server.
        """
        monkeypatch.setenv("GERRIT_BASE_URL", "https://gerrit.example.org/r")
        fake = FakeGitHub(orgs={"testorg": ["repo1"]})

        names = _discover(
            fake, "github.com/testorg", base_url="https://gerrit.example.org/r"
        )

        assert names == ["repo1"]
        urls = [u for u, _ in fake.posts] + [u for _, u in fake.requests]
        assert _hosts(urls) == {"https://api.github.com"}


class TestOwnerKinds:
    """#320: github.com/LOGIN discovers what LOGIN owns, user or org."""

    @staticmethod
    def _fake() -> FakeGitHub:
        """An org, a user, and a token owner in many repositories."""
        return FakeGitHub(
            orgs={"someorg": ["org-repo"]},
            users={"someuser": ["Hello-World", "Spoon-Knife"]},
            token_owner_repos=[
                ("tokenowner", "private-notes"),
                ("someorg", "org-repo"),
                ("otherorg", ".github"),
                ("thirdorg", ".github"),
            ],
        )

    @pytest.mark.parametrize("login", ["someuser", "someorg"])
    def test_login_travels_as_a_variable(self, login: str) -> None:
        """The login is a GraphQL variable, never spliced into the query."""
        fake = self._fake()

        _discover(fake, f"github.com/{login}")

        (_url, query), *_rest = fake.posts
        assert login not in query

    def test_user_account_lists_that_users_repositories(self, no_sleep: Mock) -> None:
        """A user login yields that user's repositories only."""
        fake = self._fake()

        names = _discover(fake, "github.com/someuser")

        assert names == ["Hello-World", "Spoon-Knife"]
        assert not any(
            u.split("?")[0].endswith("/user/repos") for _, u in fake.requests
        )

    def test_user_account_takes_no_retries(self, no_sleep: Mock) -> None:
        """Recognising a user account costs no retry backoff."""
        fake = self._fake()

        _discover(fake, "github.com/someuser")

        no_sleep.assert_not_called()

    def test_organisation_lists_its_repositories(self, no_sleep: Mock) -> None:
        """An organisation login yields the organisation's repositories."""
        fake = self._fake()

        names = _discover(fake, "github.com/someorg")

        assert names == ["org-repo"]
        no_sleep.assert_not_called()

    def test_unknown_login_is_a_clear_error(self, no_sleep: Mock) -> None:
        """A login GitHub doesn't know fails, naming the login."""
        fake = self._fake()

        with pytest.raises(GitHubNotFoundError, match="no-such-login"):
            _discover(fake, "github.com/no-such-login")

        no_sleep.assert_not_called()
        assert not fake.requests

    def test_rejected_token_fails_discovery(self, no_sleep: Mock) -> None:
        """A token GitHub rejects fails discovery, not an empty success."""
        rejected = _response({"message": "Bad credentials"}, status=401)
        cfg = Config(
            host="github.com/someorg",
            source_type=SourceType.GITHUB,
            github_token="expired-token",
        )

        with (
            patch("httpx.Client.post", return_value=rejected),
            pytest.raises(GitHubAuthError),
        ):
            discover_github_repositories(cfg)

        no_sleep.assert_not_called()
