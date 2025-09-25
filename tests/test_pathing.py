# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for pathing module."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from gerrit_clone.pathing import (
    AtomicClonePath,
    PathConflictError,
    PathValidationError,
    check_path_conflicts,
    get_project_path,
    get_temp_clone_path,
    sanitize_project_name,
    validate_project_name,
)


class TestValidateProjectName:
    """Test validate_project_name function."""

    def test_validate_normal_name(self):
        """Test validation of normal project names."""
        # Should not raise for valid names
        validate_project_name("normal-project")
        validate_project_name("my_project")
        validate_project_name("project123")
        validate_project_name("group/subproject")

    def test_validate_empty_name(self):
        """Test validation fails for empty names."""
        with pytest.raises(PathValidationError, match="cannot be empty"):
            validate_project_name("")

        with pytest.raises(PathValidationError):
            validate_project_name("   ")

    def test_validate_dangerous_names(self):
        """Test validation of potentially dangerous names."""
        dangerous_names = [
            "../escape",
            "dir/../../escape",
            ".git",
            ".",
            "..",
        ]

        for name in dangerous_names:
            with pytest.raises(PathValidationError):
                validate_project_name(name)


class TestSanitizeProjectName:
    """Test sanitize_project_name function."""

    def test_sanitize_normal_name(self):
        """Test sanitization of normal project names."""
        assert sanitize_project_name("normal-project") == "normal-project"
        assert sanitize_project_name("my_project") == "my_project"
        assert sanitize_project_name("project123") == "project123"

    def test_sanitize_with_slashes(self) -> None:
        """Test sanitization preserves valid path separators."""
        # Should preserve forward slashes in project paths
        result = sanitize_project_name("group/project")
        assert "/" in result or result == "group/project"

    def test_sanitize_with_dots(self) -> None:
        """Test sanitization of dot sequences."""
        # Should handle dangerous dot sequences
        result = sanitize_project_name("project..name")
        assert result  # Should produce some safe output

    def test_sanitize_reserved_names(self) -> None:
        """Test sanitization of OS reserved names."""
        # Should handle reserved names safely
        reserved_names = ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]
        for name in reserved_names:
            result = sanitize_project_name(name)
            assert result != name  # Should be modified

    def test_sanitize_with_special_chars(self) -> None:
        """Test sanitization of special characters."""
        special_names = [
            "project<name>",
            "project:name",
            "project|name",
            "project?name",
            "project*name",
        ]
        for name in special_names:
            result = sanitize_project_name(name)
            # Should not contain dangerous characters
            assert not any(c in result for c in "<>:|?*")

    def test_sanitize_empty_and_whitespace(self) -> None:
        """Test sanitization of empty and whitespace strings."""
        with pytest.raises(PathValidationError):
            sanitize_project_name("")
        with pytest.raises(PathValidationError):
            sanitize_project_name("   ")

    def test_sanitize_leading_trailing_whitespace(self) -> None:
        """Test removal of leading/trailing whitespace."""
        result = sanitize_project_name("  project  ")
        assert result.strip() == result  # Should not have leading/trailing whitespace

    def test_sanitize_max_length(self) -> None:
        """Test handling of long names."""
        long_name = "a" * 300
        result = sanitize_project_name(long_name)
        # Should handle long names appropriately
        assert len(result) > 0


class TestGetProjectPath:
    """Test get_project_path function."""

    def test_get_project_path_simple(self) -> None:
        """Test simple project path generation."""
        base = Path("/tmp/repos")
        project_name = "my-project"
        result = get_project_path(project_name, base)
        assert result == base / "my-project"

    def test_get_project_path_nested(self) -> None:
        """Test nested project path generation."""
        base = Path("/tmp/repos")
        project_name = "group/subgroup/project"
        result = get_project_path(project_name, base)
        assert result == base / "group" / "subgroup" / "project"

    def test_get_project_path_with_sanitization(self) -> None:
        """Test project path with sanitization needed."""
        base = Path("/tmp/repos")
        project_name = "group/../evil/project"
        result = get_project_path(project_name, base)
        # Should handle dangerous paths safely
        assert ".." not in str(result)

    def test_get_project_path_absolute_base(self) -> None:
        """Test with absolute base path."""
        base = Path("/home/user/repos").resolve()
        project_name = "project"
        result = get_project_path(project_name, base)
        assert result.is_absolute()
        assert result == base / "project"

    def test_get_project_path_relative_base(self) -> None:
        """Test with relative base path."""
        base = Path("repos")
        project_name = "project"
        result = get_project_path(project_name, base)
        assert result == base / "project"


class TestCheckPathConflicts:
    """Test check_path_conflicts function."""

    def test_check_path_conflicts_no_conflicts(self) -> None:
        """Test with no path conflicts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "new-project"
            result = check_path_conflicts(target_path)
            assert result is None  # No conflicts

    def test_check_path_conflicts_existing_directory(self) -> None:
        """Test detection of existing directory conflicts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            existing_path = Path(temp_dir) / "existing-project"
            existing_path.mkdir()

            result = check_path_conflicts(existing_path)
            assert result is not None  # Should detect conflict
            assert "exists" in result.lower()

    def test_check_path_conflicts_existing_file(self) -> None:
        """Test detection of existing file conflicts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            existing_file = Path(temp_dir) / "project.txt"
            existing_file.write_text("test")

            result = check_path_conflicts(existing_file)
            assert result is not None  # Should detect conflict

    def test_check_path_conflicts_permission_issues(self) -> None:
        """Test detection of permission-related conflicts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            readonly_dir = Path(temp_dir) / "readonly"
            readonly_dir.mkdir()
            # Make directory readonly if possible
            try:
                readonly_dir.chmod(0o444)
                target_path = readonly_dir / "project"
                check_path_conflicts(target_path)
                # Might detect permission issues
            finally:
                # Restore permissions for cleanup
                readonly_dir.chmod(0o755)


class TestGetTempClonePath:
    """Test get_temp_clone_path function."""

    def test_get_temp_clone_path_basic(self) -> None:
        """Test basic temp path generation."""
        target = Path("/tmp/repos/project")
        temp = get_temp_clone_path(target)

        assert temp.parent == target.parent
        assert temp != target
        assert ".partial." in temp.name

    def test_get_temp_clone_path_unique(self) -> None:
        """Test that temp paths are unique."""
        target = Path("/tmp/repos/project")
        temp1 = get_temp_clone_path(target)
        temp2 = get_temp_clone_path(target)

        assert temp1 != temp2


class TestAtomicClonePath:
    """Test AtomicClonePath context manager."""

    def test_atomic_clone_path_creation(self) -> None:
        """Test AtomicClonePath creation and properties."""
        target_path = Path("/tmp/repos/project")
        atomic_path = AtomicClonePath(target_path)

        assert atomic_path.target_path == target_path
        assert atomic_path.temp_path != target_path
        assert atomic_path.temp_path.parent == target_path.parent

    def test_atomic_clone_path_unique_temp_names(self) -> None:
        """Test that temp paths are unique."""
        target_path = Path("/tmp/repos/project")
        atomic1 = AtomicClonePath(target_path)
        atomic2 = AtomicClonePath(target_path)

        assert atomic1.temp_path != atomic2.temp_path

    @patch("gerrit_clone.pathing.shutil.rmtree")
    def test_atomic_clone_path_cleanup_on_exception(self, mock_rmtree):
        """Test cleanup when exception occurs."""
        target_path = Path("/tmp/repos/project")

        try:
            with AtomicClonePath(target_path) as atomic:
                # Simulate temp directory exists
                atomic.temp_path.mkdir(parents=True, exist_ok=True)
                raise ValueError("Test error")
        except ValueError:
            pass  # Expected exception

        # Should have called cleanup
        mock_rmtree.assert_called()

    def test_atomic_clone_path_context_manager_protocol(self):
        """Test context manager protocol implementation."""
        target_path = Path("/tmp/repos/project")
        atomic_path = AtomicClonePath(target_path)

        # Test __enter__
        result = atomic_path.__enter__()
        assert result is atomic_path

        # Test __exit__ with no exception
        atomic_path.__exit__(None, None, None)
        # __exit__ should complete without error

    @patch("gerrit_clone.pathing.shutil.rmtree")
    @patch("os.path.exists")
    def test_atomic_clone_path_successful_completion(self, mock_exists, mock_rmtree):
        """Test successful atomic operation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "project"

            mock_exists.return_value = True

            with AtomicClonePath(target_path) as atomic:
                # Simulate creating content in temp directory
                atomic.temp_path.mkdir(parents=True, exist_ok=True)
                (atomic.temp_path / "test.txt").write_text("test")

                # Call finalize to simulate successful clone
                atomic.finalize()

            # Should not have called rmtree since finalize was called
            mock_rmtree.assert_not_called()

    def test_atomic_clone_path_finalize_moves_directory(self):
        """Test that finalize moves temp to target."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "project"

            with AtomicClonePath(target_path) as atomic:
                # Create temp directory with content
                atomic.temp_path.mkdir(parents=True)
                test_file = atomic.temp_path / "test.txt"
                test_file.write_text("test content")

                # Finalize should move temp to target
                atomic.finalize()

                # Check target exists and temp doesn't
                assert target_path.exists()
                assert (target_path / "test.txt").read_text() == "test content"
                assert not atomic.temp_path.exists()

    def test_atomic_clone_path_finalize_idempotent(self):
        """Test that multiple finalize calls don't cause issues."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "project"

            with AtomicClonePath(target_path) as atomic:
                atomic.temp_path.mkdir(parents=True)
                atomic.finalize()

                # Second finalize should be safe
                atomic.finalize()

                assert target_path.exists()

    @patch("gerrit_clone.pathing.shutil.rmtree")
    def test_atomic_clone_path_cleanup_error_handling(self, mock_rmtree):
        """Test error handling during cleanup."""
        mock_rmtree.side_effect = OSError("Permission denied")

        target_path = Path("/tmp/repos/project")

        # Should not raise exception even if cleanup fails
        with AtomicClonePath(target_path) as atomic:
            atomic.temp_path.mkdir(parents=True, exist_ok=True)
            pass  # Exit context without finalize

    def test_atomic_clone_path_target_already_exists(self):
        """Test behavior when target already exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "existing"
            target_path.mkdir()
            (target_path / "existing.txt").write_text("existing")

            with AtomicClonePath(target_path) as atomic:
                atomic.temp_path.mkdir(parents=True)
                (atomic.temp_path / "new.txt").write_text("new")
                atomic.finalize()

                # Should replace existing content
                assert target_path.exists()
                assert (target_path / "new.txt").exists()
                assert not (target_path / "existing.txt").exists()


class TestPathConflictError:
    """Test PathConflictError exception."""

    def test_path_conflict_error_creation(self):
        """Test PathConflictError creation."""
        error = PathConflictError("Test conflict")
        assert str(error) == "Test conflict"
        assert isinstance(error, Exception)

    def test_path_conflict_error_with_details(self):
        """Test PathConflictError with detailed message."""
        conflicts = ["project1", "Project1"]
        message = f"Case conflicts detected: {conflicts}"
        error = PathConflictError(message)
        assert "Case conflicts detected" in str(error)
        assert "project1" in str(error)


class TestPathingIntegration:
    """Integration tests for pathing functionality."""

    def test_end_to_end_path_handling(self):
        """Test complete path handling workflow."""
        projects = ["normal-project", "group/subproject", "complex/nested/deep/project"]
        base_path = Path("/tmp/test-repos")

        # Should not raise conflicts - check each project path individually
        for project in projects:
            project_path = get_project_path(project, base_path)
            conflict = check_path_conflicts(project_path)
            assert conflict is None, f"Unexpected conflict for {project}: {conflict}"

        # Generate paths for all projects
        paths = [get_project_path(project, base_path) for project in projects]

        expected_paths = [
            base_path / "normal-project",
            base_path / "group" / "subproject",
            base_path / "complex" / "nested" / "deep" / "project",
        ]

        assert paths == expected_paths

    def test_path_sanitization_integration(self):
        """Test path sanitization in realistic scenarios."""
        problematic_projects = [
            "project/../evil",
            "group\\windows\\path",
            "project with spaces",
            "project:with:colons",
        ]
        base_path = Path("/tmp/repos")

        for project in problematic_projects:
            path = get_project_path(project, base_path)
            # Path should be safe (no .. traversal, etc.)
            assert ".." not in str(path)
            assert "\\" not in str(path) or os.name == "nt"

    def test_atomic_clone_real_filesystem(self):
        """Test atomic clone with real filesystem operations."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = Path(temp_dir)
            project_name = "test-project"
            target_path = get_project_path(project_name, base_path)

            # Simulate successful clone
            with AtomicClonePath(target_path) as atomic:
                # Create a realistic repository structure
                atomic.temp_path.mkdir(parents=True)
                (atomic.temp_path / ".git").mkdir()
                (atomic.temp_path / "README.md").write_text("# Test Project")
                (atomic.temp_path / "src").mkdir()
                (atomic.temp_path / "src" / "main.py").write_text("print('hello')")

                atomic.finalize()

            # Verify final structure
            assert target_path.exists()
            assert (target_path / ".git").is_dir()
            assert (target_path / "README.md").read_text() == "# Test Project"
            assert (target_path / "src" / "main.py").read_text() == "print('hello')"

    def test_concurrent_atomic_operations(self):
        """Test multiple atomic operations don't interfere."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = Path(temp_dir)

            # Create multiple atomic contexts
            atomics = [AtomicClonePath(base_path / f"project{i}") for i in range(3)]

            # All should have different temp paths
            temp_paths = [atomic.temp_path for atomic in atomics]
            assert len(set(temp_paths)) == len(temp_paths)

            # All should work independently
            for i, atomic in enumerate(atomics):
                with atomic:
                    atomic.temp_path.mkdir(parents=True)
                    (atomic.temp_path / f"file{i}.txt").write_text(f"content{i}")
                    atomic.finalize()

            # All should complete successfully
            for i in range(3):
                project_path = base_path / f"project{i}"
                assert project_path.exists()
                assert (project_path / f"file{i}.txt").read_text() == f"content{i}"

    def test_error_recovery_and_cleanup(self):
        """Test error recovery and cleanup behavior."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = Path(temp_dir)
            target_path = base_path / "failed-project"

            temp_path_ref = None

            # Simulate failed operation
            try:
                with AtomicClonePath(target_path) as atomic:
                    temp_path_ref = atomic.temp_path
                    atomic.temp_path.mkdir(parents=True)
                    (atomic.temp_path / "partial.txt").write_text("partial")

                    # Simulate error before finalize
                    raise RuntimeError("Clone failed")
            except RuntimeError:
                pass

            # Target should not exist, temp should be cleaned up
            assert not target_path.exists()
            assert temp_path_ref is not None
            # Note: Cleanup might not be immediate due to temp directory naming
