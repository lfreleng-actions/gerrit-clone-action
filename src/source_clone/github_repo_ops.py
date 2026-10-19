# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Synchronous single-repository and account operations.

Each method here maps one REST endpoint onto the client's own types,
delegating all HTTP concerns to :class:`GitHubTransport`.
"""

from __future__ import annotations

from typing import Any

from source_clone.github_models import (
    GitHubAPIError,
    GitHubNotFoundError,
    GitHubRepo,
    build_create_repo_payload,
)
from source_clone.github_transport import GitHubTransport
from source_clone.logging import get_logger

logger = get_logger(__name__)


class GitHubRepoOperations(GitHubTransport):
    """REST operations against a single repository or account."""

    def get_authenticated_user(self) -> dict[str, Any]:
        """Get the authenticated user information.

        Returns:
            User information dictionary

        Raises:
            GitHubAPIError: For API errors
        """
        data = self._request("GET", "/user")
        if not isinstance(data, dict):
            raise GitHubAPIError("Unexpected response type for user info")
        return data

    def get_user_orgs(self) -> list[dict[str, Any]]:
        """Get organizations for the authenticated user.

        Returns:
            List of organization dictionaries

        Raises:
            GitHubAPIError: For API errors
        """
        data = self._request("GET", "/user/orgs")
        if not isinstance(data, list):
            raise GitHubAPIError("Unexpected response type for user orgs")
        return data

    def repo_exists(self, owner: str, repo_name: str) -> bool:
        """Check if a repository exists.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Returns:
            True if repository exists, False otherwise
        """
        try:
            self._request("GET", f"/repos/{owner}/{repo_name}")
            return True
        except GitHubNotFoundError:
            return False
        except GitHubAPIError as e:
            logger.warning(f"Error checking repository existence: {e}")
            return False

    def get_repo(self, owner: str, repo_name: str) -> GitHubRepo:
        """Get repository information.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Returns:
            GitHubRepo instance

        Raises:
            GitHubNotFoundError: If repository not found
            GitHubAPIError: For other API errors
        """
        data = self._request("GET", f"/repos/{owner}/{repo_name}")
        if not isinstance(data, dict):
            raise GitHubAPIError("Unexpected response type for repo info")
        return GitHubRepo.from_api_response(data)

    def create_repo(
        self,
        name: str,
        org: str | None = None,
        description: str | None = None,
        private: bool = False,
    ) -> GitHubRepo:
        """Create a new repository.

        Args:
            name: Repository name
            org: Organization name (if None, creates in user account)
            description: Repository description
            private: Whether repository should be private

        Returns:
            Created GitHubRepo instance

        Raises:
            GitHubAPIError: For API errors
        """
        payload = build_create_repo_payload(name, description, private)

        endpoint = f"/orgs/{org}/repos" if org else "/user/repos"

        logger.info(f"Creating GitHub repository: {org}/{name}" if org else name)
        data = self._request("POST", endpoint, json=payload)
        if not isinstance(data, dict):
            raise GitHubAPIError("Unexpected response type for repo creation")
        return GitHubRepo.from_api_response(data)

    def list_repos(
        self,
        org: str | None = None,
        per_page: int = 100,
    ) -> list[GitHubRepo]:
        """List repositories for user or organization.

        Args:
            org: Organization name (if None, lists user repos)
            per_page: Number of results per page

        Returns:
            List of GitHubRepo instances

        Raises:
            GitHubAPIError: For API errors
        """
        repos: list[GitHubRepo] = []
        page = 1

        while True:
            endpoint = f"/orgs/{org}/repos" if org else "/user/repos"
            endpoint += f"?per_page={per_page}&page={page}"

            data = self._request("GET", endpoint)
            if not isinstance(data, list):
                raise GitHubAPIError("Unexpected response type for repo list")

            if not data:
                break

            repos.extend(GitHubRepo.from_api_response(r) for r in data)
            page += 1

            if len(data) < per_page:
                break

        return repos

    def set_default_branch(self, owner: str, repo_name: str, branch: str) -> bool:
        """Set the default branch for a repository.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name
            branch: Branch name to set as default

        Returns:
            True if successful, False otherwise
        """
        logger.debug(
            "Setting default branch for %s/%s to '%s'",
            owner,
            repo_name,
            branch,
        )
        try:
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo_name}",
                json={"default_branch": branch},
            )
            logger.info(
                "Set default branch for %s/%s to '%s'",
                owner,
                repo_name,
                branch,
            )
            return True
        except GitHubAPIError as exc:
            logger.warning(
                "Failed to set default branch for %s/%s to '%s': %s",
                owner,
                repo_name,
                branch,
                exc,
            )
            return False

    def delete_repo(self, owner: str, repo_name: str) -> None:
        """Delete a repository.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Raises:
            GitHubAPIError: For API errors
        """
        self._request("DELETE", f"/repos/{owner}/{repo_name}")


def get_default_org_or_user(
    api: GitHubRepoOperations,
) -> tuple[str, bool]:
    """Get default organization or user for the authenticated token.

    Returns the first organization if available, otherwise returns
    the authenticated user's login.

    Args:
        api: GitHubAPI instance

    Returns:
        Tuple of (owner_name, is_org)

    Raises:
        GitHubAPIError: For API errors
    """
    orgs = api.get_user_orgs()
    if orgs:
        org_login = orgs[0]["login"]
        logger.info(f"Using default organization: {org_login}")
        return org_login, True

    user = api.get_authenticated_user()
    user_login = user["login"]
    logger.info(f"Using authenticated user account: {user_login}")
    return user_login, False
