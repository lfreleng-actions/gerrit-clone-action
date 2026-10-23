# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Batch deletion of GitHub repositories during an organization reset.

Filters out invalid repository names before delegating to the GitHub API
batch delete helper, then reports per-repository outcomes.
"""

from __future__ import annotations

from source_clone.reset_confirmation import ResetConfirmationBase


class ResetDeletionBase(ResetConfirmationBase):
    """Destructive repository deletion for :class:`ResetManager`."""

    async def delete_all_repos(
        self,
        repo_names: list[str],
    ) -> dict[str, tuple[bool, str | None]]:
        """
        Delete all repositories in the organization.

        Args:
            repo_names: List of repository names to delete

        Returns:
            Dictionary mapping repo name to (success, error_message)
        """
        invalid_names: dict[str, str] = {}
        valid_names: list[str] = []

        for name in repo_names:
            is_valid, error = self._validate_repo_name(name)
            if not is_valid:
                invalid_names[name] = error or "Invalid repository name"
            else:
                valid_names.append(name)

        if invalid_names:
            self.console.print(
                f"\n⚠️  [yellow]Skipping {len(invalid_names)} invalid repository names:[/yellow]"
            )
            for name, error in list(invalid_names.items())[:5]:
                self.console.print(f"  - {name}: {error}")
            if len(invalid_names) > 5:
                self.console.print(f"  ... and {len(invalid_names) - 5} more")

        if not valid_names:
            self.console.print("\n❌ No valid repository names to delete")
            return {name: (False, error) for name, error in invalid_names.items()}

        self.console.print(f"\n🗑️  Deleting {len(valid_names)} repositories...")

        # Use existing batch_delete_repos from github_api.py
        results = await self.github_api.batch_delete_repos(
            owner=self.org,
            repo_names=valid_names,
            max_concurrent=10,
        )

        # Merge invalid names into results
        for name, error in invalid_names.items():
            results[name] = (False, error)

        success_count = sum(1 for success, _ in results.values() if success)
        failed_count = len(results) - success_count

        if failed_count > 0:
            failed_repos = [
                name for name, (success, _) in results.items() if not success
            ]
            self.console.print(
                f"\n⚠️  [yellow]Failed to delete {failed_count} repositories:[/yellow]"
            )
            for name in failed_repos[:5]:  # Show first 5
                _, error = results[name]
                self.console.print(f"  - {name}: {error}")

            if len(failed_repos) > 5:
                self.console.print(f"  ... and {len(failed_repos) - 5} more")

        self.console.print(
            f"\n✅ Successfully deleted {success_count}/{len(results)} repositories"
        )

        return results


__all__ = [
    "ResetDeletionBase",
]
