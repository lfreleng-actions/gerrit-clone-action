# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
# ruff: noqa: SIM117

"""
Comprehensive tests for the netrc module.

Tests cover parsing, credential lookup, file discovery, permissions
checking, and edge cases for .netrc file handling.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from gerrit_clone.netrc import (
    NetrcCredentials,
    NetrcParseError,
    NetrcParser,
    _normalize_host_for_netrc_lookup,
    check_netrc_permissions,
    find_netrc_file,
    get_credentials_for_host,
    load_netrc,
)


class TestNormalizeHostForNetrcLookup:
    """Tests for _normalize_host_for_netrc_lookup helper function."""

    def test_simple_hostname(self) -> None:
        """Test that simple hostnames pass through unchanged."""
        result = _normalize_host_for_netrc_lookup("gerrit.example.org")
        assert result == "gerrit.example.org"

    def test_uppercase_hostname(self) -> None:
        """Test that uppercase hostnames are lowercased."""
        result = _normalize_host_for_netrc_lookup("GERRIT.EXAMPLE.ORG")
        assert result == "gerrit.example.org"

    def test_hostname_with_whitespace(self) -> None:
        """Test that whitespace is stripped from hostnames."""
        result = _normalize_host_for_netrc_lookup("  gerrit.example.org  ")
        assert result == "gerrit.example.org"

    def test_hostname_with_https_scheme(self) -> None:
        """Test that https:// scheme is stripped."""
        result = _normalize_host_for_netrc_lookup("https://gerrit.example.org")
        assert result == "gerrit.example.org"

    def test_hostname_with_http_scheme(self) -> None:
        """Test that http:// scheme is stripped."""
        result = _normalize_host_for_netrc_lookup("http://gerrit.example.org")
        assert result == "gerrit.example.org"

    def test_hostname_with_port(self) -> None:
        """Test that port number is stripped."""
        result = _normalize_host_for_netrc_lookup("gerrit.example.org:8080")
        assert result == "gerrit.example.org"

    def test_hostname_with_path(self) -> None:
        """Test that path is stripped."""
        result = _normalize_host_for_netrc_lookup("gerrit.example.org/r")
        assert result == "gerrit.example.org"

    def test_hostname_with_scheme_and_path(self) -> None:
        """Test that scheme and path are both stripped."""
        result = _normalize_host_for_netrc_lookup("https://gerrit.example.org/r")
        assert result == "gerrit.example.org"

    def test_hostname_with_scheme_port_and_path(self) -> None:
        """Test that scheme, port, and path are all stripped."""
        result = _normalize_host_for_netrc_lookup("https://gerrit.example.org:8443/r/a")
        assert result == "gerrit.example.org"

    def test_mixed_case_with_all_components(self) -> None:
        """Test mixed case with scheme, port, and path."""
        result = _normalize_host_for_netrc_lookup(
            "HTTPS://Gerrit.Example.ORG:8080/path"
        )
        assert result == "gerrit.example.org"


class TestNetrcCredentials:
    """Tests for NetrcCredentials dataclass."""

    def test_credentials_creation(self) -> None:
        """Test creating credentials instance."""
        creds = NetrcCredentials(
            machine="gerrit.example.org",
            login="testuser",
            password="secret123",
        )
        assert creds.machine == "gerrit.example.org"
        assert creds.login == "testuser"
        assert creds.password == "secret123"

    def test_credentials_immutable(self) -> None:
        """Test that credentials are immutable (frozen)."""
        creds = NetrcCredentials(
            machine="gerrit.example.org",
            login="testuser",
            password="secret123",
        )
        with pytest.raises(AttributeError):
            creds.password = "newpassword"  # type: ignore[misc]

    def test_credentials_repr_masks_password(self) -> None:
        """Test that repr masks the password for security."""
        creds = NetrcCredentials(
            machine="gerrit.example.org",
            login="testuser",
            password="supersecret",
        )
        repr_str = repr(creds)
        assert "supersecret" not in repr_str
        assert "****" in repr_str
        assert "testuser" in repr_str
        assert "gerrit.example.org" in repr_str


class TestNetrcParserBasic:
    """Tests for basic NetrcParser functionality."""

    def test_parse_single_machine(self) -> None:
        """Test parsing a single machine entry."""
        content = """
        machine gerrit.example.org
        login myuser
        password mypass
        """
        parser = NetrcParser(content)
        creds = parser.get_credentials("gerrit.example.org")
        assert creds is not None
        assert creds.login == "myuser"
        assert creds.password == "mypass"

    def test_parse_single_line_format(self) -> None:
        """Test parsing single-line format."""
        content = "machine gerrit.example.org login myuser password mypass"
        parser = NetrcParser(content)
        creds = parser.get_credentials("gerrit.example.org")
        assert creds is not None
        assert creds.login == "myuser"
        assert creds.password == "mypass"

    def test_parse_multiple_machines(self) -> None:
        """Test parsing multiple machine entries."""
        content = """
        machine gerrit.onap.org login user1 password pass1
        machine gerrit.opendaylight.org login user2 password pass2
        machine gerrit.linuxfoundation.org login user3 password pass3
        """
        parser = NetrcParser(content)

        creds1 = parser.get_credentials("gerrit.onap.org")
        assert creds1 is not None
        assert creds1.login == "user1"
        assert creds1.password == "pass1"

        creds2 = parser.get_credentials("gerrit.opendaylight.org")
        assert creds2 is not None
        assert creds2.login == "user2"
        assert creds2.password == "pass2"

        creds3 = parser.get_credentials("gerrit.linuxfoundation.org")
        assert creds3 is not None
        assert creds3.login == "user3"
        assert creds3.password == "pass3"

    def test_parse_default_entry(self) -> None:
        """Test parsing default entry."""
        content = """
        machine gerrit.example.org login specific password specpass
        default login anonymous password guest@example.org
        """
        parser = NetrcParser(content)

        # Specific match
        creds = parser.get_credentials("gerrit.example.org")
        assert creds is not None
        assert creds.login == "specific"

        # Falls back to default
        default_creds = parser.get_credentials("unknown.server.org")
        assert default_creds is not None
        assert default_creds.login == "anonymous"
        assert default_creds.password == "guest@example.org"

    def test_machines_property(self) -> None:
        """Test machines property returns all machine names."""
        content = """
        machine server1.org login u1 password p1
        machine server2.org login u2 password p2
        """
        parser = NetrcParser(content)
        machines = parser.machines
        assert "server1.org" in machines
        assert "server2.org" in machines
        assert len(machines) == 2

    def test_has_default_property(self) -> None:
        """Test has_default property."""
        content_with_default = """
        machine server.org login u password p
        default login anon password anon
        """
        parser_with = NetrcParser(content_with_default)
        assert parser_with.has_default is True

        content_without = "machine server.org login u password p"
        parser_without = NetrcParser(content_without)
        assert parser_without.has_default is False

    def test_case_insensitive_lookup(self) -> None:
        """Test that machine lookup is case-insensitive."""
        content = "machine Gerrit.Example.Org login user password pass"
        parser = NetrcParser(content)

        creds = parser.get_credentials("gerrit.example.org")
        assert creds is not None
        assert creds.login == "user"

        creds2 = parser.get_credentials("GERRIT.EXAMPLE.ORG")
        assert creds2 is not None
        assert creds2.login == "user"


class TestNetrcParserQuotedStrings:
    """Tests for quoted string handling in NetrcParser."""

    def test_quoted_password(self) -> None:
        """Test parsing quoted password with spaces."""
        content = 'machine example.org login user password "my secret pass"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == "my secret pass"

    def test_quoted_login(self) -> None:
        """Test parsing quoted login."""
        content = 'machine example.org login "user name" password pass'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.login == "user name"

    def test_escape_sequences(self) -> None:
        """Test escape sequences in quoted strings."""
        content = r'machine example.org login user password "pass\"word"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == 'pass"word'

    def test_escape_newline(self) -> None:
        """Test newline escape sequence."""
        content = r'machine example.org login user password "line1\nline2"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == "line1\nline2"

    def test_escape_tab(self) -> None:
        """Test tab escape sequence."""
        content = r'machine example.org login user password "col1\tcol2"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == "col1\tcol2"

    def test_escape_carriage_return(self) -> None:
        """Test carriage return escape sequence."""
        content = r'machine example.org login user password "text\rmore"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == "text\rmore"

    def test_escape_backslash(self) -> None:
        """Test backslash escape sequence."""
        content = r'machine example.org login user password "path\\to\\file"'
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.password == "path\\to\\file"


class TestNetrcParserComments:
    """Tests for comment handling in NetrcParser."""

    def test_comment_lines(self) -> None:
        """Test that comment lines are ignored."""
        content = """
        # This is a comment
        machine example.org login user password pass
        # Another comment
        """
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.login == "user"

    def test_inline_comments(self) -> None:
        """Test inline comments."""
        content = """
        machine example.org login user password pass # inline comment
        """
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.login == "user"


class TestNetrcParserEdgeCases:
    """Tests for edge cases in NetrcParser."""

    def test_empty_content(self) -> None:
        """Test parsing empty content."""
        parser = NetrcParser("")
        assert parser.get_credentials("example.org") is None
        assert parser.machines == []

    def test_whitespace_only(self) -> None:
        """Test parsing whitespace-only content."""
        parser = NetrcParser("   \n\n   \t\t   ")
        assert parser.get_credentials("example.org") is None

    def test_missing_password(self) -> None:
        """Test entry with missing password is skipped."""
        content = "machine example.org login user"
        parser = NetrcParser(content)
        assert parser.get_credentials("example.org") is None

    def test_missing_login(self) -> None:
        """Test entry with missing login is skipped."""
        content = "machine example.org password pass"
        parser = NetrcParser(content)
        assert parser.get_credentials("example.org") is None

    def test_macdef_skipped(self) -> None:
        """Test that macdef entries are skipped."""
        content = """
        machine example.org login user password pass
        macdef init
        cd /home
        ls -la

        machine other.org login user2 password pass2
        """
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.login == "user"

    def test_macdef_body_containing_keywords(self) -> None:
        """Test that macdef body containing 'machine' keyword is handled.

        Per netrc spec, macdef sections end at a blank line, not when
        encountering keywords like 'machine', 'default', etc. This test
        ensures the parser doesn't prematurely end the macdef when the
        macro body contains these keywords as text.
        """
        content = """
        machine first.org login user1 password pass1
        macdef upload
        echo "Uploading to machine server"
        machine_check --verify
        default_action run

        machine second.org login user2 password pass2
        """
        parser = NetrcParser(content)

        # First machine entry should be parsed correctly
        creds1 = parser.get_credentials("first.org")
        assert creds1 is not None
        assert creds1.login == "user1"
        assert creds1.password == "pass1"

        # Second machine entry after macdef should be parsed correctly
        creds2 = parser.get_credentials("second.org")
        assert creds2 is not None
        assert creds2.login == "user2"
        assert creds2.password == "pass2"

    def test_macdef_multiple_blank_lines(self) -> None:
        """Test macdef with multiple blank lines terminates correctly."""
        content = """
        machine example.org login user password pass
        macdef test
        line1
        line2


        machine other.org login user2 password pass2
        """
        parser = NetrcParser(content)

        creds1 = parser.get_credentials("example.org")
        assert creds1 is not None
        assert creds1.login == "user"

        creds2 = parser.get_credentials("other.org")
        assert creds2 is not None
        assert creds2.login == "user2"

    def test_unknown_tokens_skipped(self) -> None:
        """Test that unknown tokens are skipped."""
        content = """
        machine example.org account myaccount login user password pass
        """
        parser = NetrcParser(content)
        creds = parser.get_credentials("example.org")
        assert creds is not None
        assert creds.login == "user"

    def test_machine_without_name_raises(self) -> None:
        """Test that machine without name raises error."""
        content = "machine"
        with pytest.raises(NetrcParseError, match="Expected machine name"):
            NetrcParser(content)

    def test_login_without_value_raises(self) -> None:
        """Test that login without value raises error."""
        content = "machine example.org login"
        with pytest.raises(NetrcParseError, match="Expected login value"):
            NetrcParser(content)

    def test_password_without_value_raises(self) -> None:
        """Test that password without value raises error."""
        content = "machine example.org login user password"
        with pytest.raises(NetrcParseError, match="Expected password value"):
            NetrcParser(content)


class TestFindNetrcFile:
    """Tests for find_netrc_file function."""

    def test_explicit_path_found(self, tmp_path: Path) -> None:
        """Test finding explicit netrc file."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")

        result = find_netrc_file(explicit_path=netrc_file)
        assert result == netrc_file

    def test_explicit_path_not_found(self, tmp_path: Path) -> None:
        """Test explicit path that doesn't exist."""
        netrc_file = tmp_path / ".netrc"
        result = find_netrc_file(explicit_path=netrc_file)
        assert result is None

    def test_local_directory_search(self, tmp_path: Path) -> None:
        """Test searching in local directory."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")

        with patch.object(Path, "cwd", return_value=tmp_path):
            result = find_netrc_file(search_local=True)
            assert result == netrc_file

    def test_home_directory_search(self, tmp_path: Path) -> None:
        """Test searching in home directory."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")

        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "cwd", return_value=Path("/nonexistent")):
                result = find_netrc_file(search_local=False)
                assert result == netrc_file

    def test_no_file_found(self, tmp_path: Path) -> None:
        """Test when no netrc file exists."""
        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "cwd", return_value=tmp_path):
                result = find_netrc_file()
                assert result is None


class TestCheckNetrcPermissions:
    """Tests for check_netrc_permissions function."""

    def test_secure_permissions(self, tmp_path: Path) -> None:
        """Test file with secure permissions."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")
        netrc_file.chmod(0o600)

        result = check_netrc_permissions(netrc_file)
        assert result is True

    def test_insecure_group_readable(self, tmp_path: Path) -> None:
        """Test file readable by group."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")
        netrc_file.chmod(0o640)

        result = check_netrc_permissions(netrc_file)
        assert result is False

    def test_insecure_world_readable(self, tmp_path: Path) -> None:
        """Test file readable by others."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine x login y password z")
        netrc_file.chmod(0o604)

        result = check_netrc_permissions(netrc_file)
        assert result is False

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test nonexistent file returns True (no warning needed)."""
        netrc_file = tmp_path / ".netrc"
        result = check_netrc_permissions(netrc_file)
        assert result is True


class TestLoadNetrc:
    """Tests for load_netrc function."""

    def test_load_valid_file(self, tmp_path: Path) -> None:
        """Test loading a valid netrc file."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine gerrit.example.org login user password pass")
        netrc_file.chmod(0o600)

        parser = load_netrc(path=netrc_file)
        assert parser is not None
        creds = parser.get_credentials("gerrit.example.org")
        assert creds is not None
        assert creds.login == "user"

    def test_load_no_file(self, tmp_path: Path) -> None:
        """Test loading when no file exists."""
        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "cwd", return_value=tmp_path):
                parser = load_netrc()
                assert parser is None

    def test_load_invalid_file_raises(self, tmp_path: Path) -> None:
        """Test loading an invalid netrc file raises error."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine")  # Invalid: missing machine name

        with pytest.raises(NetrcParseError):
            load_netrc(path=netrc_file)


class TestGetCredentialsForHost:
    """Tests for get_credentials_for_host function."""

    def test_get_credentials_success(self, tmp_path: Path) -> None:
        """Test successfully getting credentials for a host."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine gerrit.onap.org login onapuser password onappass"
        )
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.onap.org",
            netrc_file=netrc_file,
        )
        assert creds is not None
        assert creds.login == "onapuser"
        assert creds.password == "onappass"

    def test_get_credentials_with_scheme(self, tmp_path: Path) -> None:
        """Test getting credentials when host has scheme."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine gerrit.example.org login user password pass")
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="https://gerrit.example.org",
            netrc_file=netrc_file,
        )
        assert creds is not None
        assert creds.login == "user"

    def test_get_credentials_with_port(self, tmp_path: Path) -> None:
        """Test getting credentials when host has port."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine gerrit.example.org login user password pass")
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.example.org:8080",
            netrc_file=netrc_file,
        )
        assert creds is not None
        assert creds.login == "user"

    def test_get_credentials_with_path(self, tmp_path: Path) -> None:
        """Test getting credentials when host has path."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine gerrit.example.org login user password pass")
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.example.org/r/changes",
            netrc_file=netrc_file,
        )
        assert creds is not None
        assert creds.login == "user"

    def test_get_credentials_disabled(self, tmp_path: Path) -> None:
        """Test that credentials are not returned when disabled."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine gerrit.example.org login user password pass")
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.example.org",
            netrc_file=netrc_file,
            use_netrc=False,
        )
        assert creds is None

    def test_get_credentials_not_found_optional(self, tmp_path: Path) -> None:
        """Test that None is returned when file not found and optional."""
        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "cwd", return_value=tmp_path):
                creds = get_credentials_for_host(
                    host="gerrit.example.org",
                    netrc_optional=True,
                )
                assert creds is None

    def test_get_credentials_not_found_required(self, tmp_path: Path) -> None:
        """Test that error raised when file not found and required."""
        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "cwd", return_value=tmp_path):
                with pytest.raises(FileNotFoundError):
                    get_credentials_for_host(
                        host="gerrit.example.org",
                        netrc_optional=False,
                    )

    def test_get_credentials_no_match(self, tmp_path: Path) -> None:
        """Test when no matching entry exists."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine other.example.org login user password pass")
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.example.org",
            netrc_file=netrc_file,
        )
        assert creds is None

    def test_get_credentials_falls_back_to_default(self, tmp_path: Path) -> None:
        """Test that lookup falls back to default entry."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine other.org login specific password specific\n"
            "default login anonymous password anon@example.org"
        )
        netrc_file.chmod(0o600)

        creds = get_credentials_for_host(
            host="gerrit.example.org",
            netrc_file=netrc_file,
        )
        assert creds is not None
        assert creds.login == "anonymous"
        assert creds.password == "anon@example.org"


class TestNetrcRealWorldExamples:
    """Tests using real-world-like netrc file examples."""

    def test_linux_foundation_servers(self, tmp_path: Path) -> None:
        """Test with typical Linux Foundation Gerrit servers."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            """
            # Linux Foundation Gerrit servers
            machine gerrit.onap.org login lfuser password lftoken123
            machine gerrit.opendaylight.org login lfuser password odltoken456
            machine gerrit.linuxfoundation.org login lfuser password lfgtoken789
            """
        )
        netrc_file.chmod(0o600)

        parser = load_netrc(path=netrc_file)
        assert parser is not None

        onap = parser.get_credentials("gerrit.onap.org")
        assert onap is not None
        assert onap.login == "lfuser"
        assert onap.password == "lftoken123"

        odl = parser.get_credentials("gerrit.opendaylight.org")
        assert odl is not None
        assert odl.password == "odltoken456"

        lf = parser.get_credentials("gerrit.linuxfoundation.org")
        assert lf is not None
        assert lf.password == "lfgtoken789"

    def test_mixed_format_file(self, tmp_path: Path) -> None:
        """Test with mixed format entries."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            """
            # Single line format
            machine gerrit.example.org login user1 password pass1

            # Multi-line format
            machine gerrit.other.org
                login user2
                password pass2

            # Default fallback
            default login anon password "anonymous user"
            """
        )
        netrc_file.chmod(0o600)

        parser = load_netrc(path=netrc_file)
        assert parser is not None

        ex = parser.get_credentials("gerrit.example.org")
        assert ex is not None
        assert ex.login == "user1"

        other = parser.get_credentials("gerrit.other.org")
        assert other is not None
        assert other.login == "user2"

        unknown = parser.get_credentials("unknown.server.org")
        assert unknown is not None
        assert unknown.login == "anon"
        assert unknown.password == "anonymous user"
