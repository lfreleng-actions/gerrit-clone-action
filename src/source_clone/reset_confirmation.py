# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Interactive confirmation and repository name validation for resets.

Provides the UX confirmation-code mechanism that guards the destructive
reset operation, along with GitHub repository name validation.
"""

from __future__ import annotations

import random
import re

from source_clone.reset_display import ResetDisplayBase


class ResetConfirmationBase(ResetDisplayBase):
    """Confirmation prompting and repository name validation."""

    def generate_confirmation_hash(
        self,
        repo_count: int,
        total_prs: int,
        total_issues: int,
    ) -> str:
        """
        Generate the confirmation code the user must retype.

        NOTE: This is NOT a security feature. The code is a UX mechanism to:
        - Require users to think more carefully than typing Y/yes
        - Ensure users read the displayed statistics before proceeding

        The code is derived from the organisation and the counts shown in
        the warning, so it is stable for a given org state and changes as
        soon as that state does. Retrying the same reset therefore offers
        the same code; the point is that it cannot be known before the
        statistics are on screen, not that it is unpredictable.

        Args:
            repo_count: Number of repositories to delete
            total_prs: Total open PRs across all repositories
            total_issues: Total open issues across all repositories

        Returns:
            16-character alphanumeric code - for UX confirmation only
        """
        # Seed the generator from the org state shown to the user, so the
        # code tracks the displayed statistics rather than the clock.
        combined_seed = f"reset:{self.org}:{repo_count}:{total_prs}:{total_issues}"
        seed_value = sum(ord(c) for c in combined_seed)

        rng = random.Random(seed_value)

        # Generate 16-character alphanumeric code (avoiding ambiguous chars)
        chars = "23456789abcdefghjkmnpqrstuvwxyz"  # No 0, O, 1, l, i for clarity
        return "".join(rng.choices(chars, k=16))

    def prompt_for_confirmation(
        self,
        repo_count: int,
        total_prs: int,
        total_issues: int,
    ) -> bool:
        """
        Prompt user for confirmation hash.

        Args:
            repo_count: Number of repositories to delete
            total_prs: Total open PRs
            total_issues: Total open issues

        Returns:
            True if user confirmed with correct hash, False otherwise
        """
        confirmation_hash = self.generate_confirmation_hash(
            repo_count, total_prs, total_issues
        )

        self.console.print()
        self.console.print(
            f"[red]⚠️  WARNING: This will PERMANENTLY DELETE {repo_count} repositories![/red]"
        )
        self.console.print(f"Organization: [cyan]{self.org}[/cyan]")
        self.console.print(f"Open PRs that will be lost: [yellow]{total_prs}[/yellow]")
        self.console.print(
            f"Open Issues that will be lost: [magenta]{total_issues}[/magenta]"
        )
        self.console.print()
        self.console.print(f"To proceed, enter: [green]{confirmation_hash}[/green]")

        try:
            user_input = input(
                "Enter the hash above to continue (or press Enter to cancel): "
            ).strip()

            if user_input == confirmation_hash:
                self.console.print("✅ Confirmation received")
                return True
            elif user_input == "":
                self.console.print("❌ Reset cancelled by user")
                return False
            else:
                self.console.print("❌ Invalid hash. Reset cancelled.")
                return False
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n❌ Reset cancelled by user")
            return False

    def _validate_repo_name(self, name: str) -> tuple[bool, str | None]:
        """
        Validate a GitHub repository name.

        GitHub repository names must:
        - Not be empty
        - Contain only alphanumeric characters, hyphens, underscores, and dots
        - Not start or end with a hyphen or underscore
        - May start with a dot (e.g. ``.github`` for org-level config)
        - Be between 1 and 100 characters

        Args:
            name: Repository name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not name or not name.strip():
            return False, "Repository name cannot be empty"

        name = name.strip()

        if len(name) > 100:
            return False, "Repository name exceeds 100 characters"

        # GitHub allows alphanumeric, hyphens, underscores, and dots.
        # Names may start with a dot (e.g. ".github") but must not
        # start or end with a hyphen or underscore.
        if not re.match(r"^\.?[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$", name):
            return False, "Repository name contains invalid characters or format"

        return True, None


__all__ = [
    "ResetConfirmationBase",
]
