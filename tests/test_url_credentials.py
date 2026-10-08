# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The one grammar for credentials in git URLs, and their redaction."""

from __future__ import annotations

import pytest

from gerrit_clone.url_credentials import (
    REDACTED,
    UNRECOGNISED_URL,
    helper_address,
    redact_text,
    redact_url,
)

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "1f2e3d4c5b6a" * 2


class TestHelperAddress:
    @pytest.mark.parametrize(
        ("url", "address"),
        [
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("helper::https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("outer::inner::ssh://h/r", "ssh://h/r"),
            ("git+ssh::h:r", "h:r"),
            ("git@host::r", "git@host::r"),
            ("/srv/git/r.git", "/srv/git/r.git"),
        ],
        ids=["plain", "helper", "nested", "scheme-chars", "not-a-transport", "path"],
    )
    def test_it_finds_what_the_helper_receives(self, url: str, address: str) -> None:
        assert helper_address(url) == address


class TestRedactUrl:
    @pytest.mark.parametrize(
        ("url", "redacted"),
        [
            (f"https://user:{TOKEN}@host/o/r.git", "https://host/o/r.git"),
            (f"https://{TOKEN}@host/o/r.git", "https://host/o/r.git"),
            (
                f"https://host/o/r.git?access_token={TOKEN}#{TOKEN}",
                "https://host/o/r.git",
            ),
            (f"https://u:{TOKEN}@[2001:db8::1]:8443/r", "https://[2001:db8::1]:8443/r"),
            (f"https://u:{TOKEN}@[unclosed/r", "https://[unclosed/r"),
            ("ssh://git@host:29418/r", "ssh://host:29418/r"),
            (f"//u:{TOKEN}@host/r", "//host/r"),
            ("builder@host:org/r.git", "host:org/r.git"),
            (f"{TOKEN}@[2001:db8::1]:r", "[2001:db8::1]:r"),
            (f"u:{TOKEN}@host:r", "host:r"),
            (f"helper::https://u:{TOKEN}@host/r", "helper::https://host/r"),
            (f"a::b::https://{TOKEN}@host/r", "a::b::https://host/r"),
            ("file:///srv/git/r.git", "file:///srv/git/r.git"),
            ("/srv/git/r.git", "/srv/git/r.git"),
            ("r.git", "r.git"),
            (f"1http://u:{TOKEN}@host/r", UNRECOGNISED_URL),
            (f"/srv/{TOKEN}@x/r", UNRECOGNISED_URL),
        ],
        ids=[
            "userinfo",
            "user-only",
            "query-and-fragment",
            "ipv6",
            "unparsable-authority",
            "ssh-user",
            "network-path",
            "scp-like",
            "scp-like-ipv6",
            "scp-like-user-and-token",
            "helper",
            "nested-helper",
            "file",
            "path",
            "bare-name",
            "unrecognised-scheme",
            "path-with-at",
        ],
    )
    def test_nothing_that_can_carry_a_credential_is_kept(
        self, url: str, redacted: str
    ) -> None:
        assert redact_url(url) == redacted
        assert TOKEN not in redact_url(url)


class TestRedactText:
    def test_secrets_and_quoted_userinfo_are_hidden(self) -> None:
        text = (
            f"fatal: unable to access 'https://user:{TOKEN}@github.com/o/r.git/' "
            f"with configured-secret"
        )

        redacted = redact_text(text, ["configured-secret", None, ""])

        assert TOKEN not in redacted
        assert "configured-secret" not in redacted
        assert f"https://{REDACTED}@github.com/o/r.git/" in redacted

    def test_text_without_any_is_unchanged(self) -> None:
        text = "Everything up-to-date\nTo https://github.com/o/r.git"

        assert redact_text(text, ["absent"]) == text
