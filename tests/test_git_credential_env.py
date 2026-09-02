# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the shared git credential environment.

The clone path and the mirror push path must authenticate the same way:
through ``GIT_CONFIG_*`` environment variables, never through a token
embedded in a URL that ``git`` then receives as an argument.
"""

from __future__ import annotations

import base64

import pytest

from gerrit_clone.git_credential_env import (
    build_token_auth_env,
    git_config_env,
    token_auth_config,
)
from gerrit_clone.github_clone_env import build_git_env
from gerrit_clone.github_clone_url import (
    UnsafeCloneUrlError,
    resolve_clone_url,
    trusted_clone_origin,
)
from gerrit_clone.mirror_push import PushSettings, build_push_env
from gerrit_clone.models import Config, Project, ProjectState, SourceType

TOKEN = "ghp_notarealtoken"
ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
HTTPS_URL = "https://github.com/org/repo.git"
#: The credential is scoped to the URL's origin, not global.
AUTH_KEY = "http.https://github.com.extraheader"
AUTH_VALUE = f"AUTHORIZATION: basic {ENCODED}"


def _project(clone_url: str | None = None) -> Project:
    return Project(
        name="org/repo",
        state=ProjectState.ACTIVE,
        source_type=SourceType.GITHUB,
        clone_url=clone_url,
    )


def _config(**kwargs: object) -> Config:
    return Config(
        host="github.com/org",
        source_type=SourceType.GITHUB,
        **kwargs,  # type: ignore[arg-type]
    )


class TestGitConfigEnv:
    """GIT_CONFIG_COUNT must always agree with the entries present."""

    def test_no_entries_produces_no_variables(self) -> None:
        """A bare GIT_CONFIG_COUNT would send git looking for absent keys."""
        assert git_config_env([]) == {}

    def test_entries_are_numbered_from_zero(self) -> None:
        env = git_config_env([("a.b", "1"), ("c.d", "2")])

        assert env == {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "a.b",
            "GIT_CONFIG_VALUE_0": "1",
            "GIT_CONFIG_KEY_1": "c.d",
            "GIT_CONFIG_VALUE_1": "2",
        }

    def test_token_is_encoded_not_carried_verbatim(self) -> None:
        key, value = token_auth_config(TOKEN, HTTPS_URL)

        assert key == AUTH_KEY
        assert value == AUTH_VALUE
        assert TOKEN not in value

    def test_the_credential_is_scoped_to_the_urls_origin(self) -> None:
        """A global ``http.extraheader`` goes to whatever host git reaches.

        Scoping bounds where the credential can travel; whether the
        origin deserves it at all is a separate check, in
        :func:`assert_trusted_origin`.
        """
        key, _ = token_auth_config(TOKEN, "https://github.com:8443/repo.git")

        assert key == "http.https://github.com:8443.extraheader"
        assert key != "http.extraheader"

    def test_no_token_and_no_extras_produces_no_variables(self) -> None:
        assert build_token_auth_env(None) == {}

    def test_a_token_without_its_url_is_refused(self) -> None:
        """Scoping is not optional, so it cannot be forgotten silently."""
        with pytest.raises(ValueError, match="scoped"):
            build_token_auth_env(TOKEN)

    def test_extras_share_one_count_block_with_the_token(self) -> None:
        """A second block would overwrite GIT_CONFIG_COUNT."""
        env = build_token_auth_env(TOKEN, HTTPS_URL, [("credential.helper", "")])

        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_KEY_1"] == AUTH_KEY


class TestSharedByCloneAndPush:
    """Both paths must produce the same credential environment."""

    def test_push_and_clone_agree_on_the_auth_header(self) -> None:
        push = build_push_env(
            PushSettings(github_token=TOKEN, clone_timeout=1, git_ssh_command=None),
            HTTPS_URL,
        )
        clone = build_git_env(_config(use_https=True, github_token=TOKEN), HTTPS_URL)

        assert push["GIT_CONFIG_KEY_0"] == AUTH_KEY
        assert push["GIT_CONFIG_VALUE_0"] == AUTH_VALUE
        assert clone["GIT_CONFIG_KEY_1"] == AUTH_KEY
        assert clone["GIT_CONFIG_VALUE_1"] == AUTH_VALUE

    def test_push_without_a_token_falls_back_to_ssh(self) -> None:
        env = build_push_env(
            PushSettings(
                github_token=None, clone_timeout=1, git_ssh_command="ssh -i key"
            ),
            "git@github.com:org/repo.git",
        )

        assert env == {"GIT_SSH_COMMAND": "ssh -i key"}


class TestCloneUrlIsCredentialFree:
    """The token must not reach argv on any clone path."""

    def test_https_url_carries_no_token(self) -> None:
        url = resolve_clone_url(
            _project("https://github.com/org/repo.git"),
            _config(use_https=True, github_token=TOKEN),
        )

        assert url == "https://github.com/org/repo.git"
        assert TOKEN not in url

    def test_ssh_url_is_preferred_when_https_not_requested(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/repo.git",
            ssh_url_override="git@github.com:org/repo.git",
        )

        url = resolve_clone_url(project, _config(github_token=TOKEN))

        assert url == "git@github.com:org/repo.git"

    def test_plaintext_url_is_never_given_the_credential(self) -> None:
        """An http:// remote would send the token in clear.

        It is cloned unauthenticated rather than refused: the token is
        withheld, so there is nothing for a foreign host to receive.
        """
        plaintext = "http://github.example.org/org/repo.git"
        url = resolve_clone_url(
            _project(plaintext), _config(use_https=True, github_token=TOKEN)
        )
        env = build_git_env(_config(use_https=True, github_token=TOKEN), url)

        assert url == plaintext
        assert "GIT_CONFIG_COUNT" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_https_without_a_token_leaves_credential_helpers_alone(self) -> None:
        """Without a token the helper *is* the authentication.

        Suppressing it would leave the documented "HTTPS with credential
        helper" mode with no way to authenticate at all.
        """
        env = build_git_env(_config(use_https=True), HTTPS_URL)

        assert "GIT_CONFIG_COUNT" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_a_token_supersedes_the_credential_helper(self) -> None:
        env = build_git_env(_config(use_https=True, github_token=TOKEN), HTTPS_URL)

        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""

    def test_the_https_fallback_still_gets_the_credential(self) -> None:
        """``use_https`` is not the only way to end up on HTTPS.

        A project with no SSH URL falls back to an HTTPS clone, and a
        private repository on that path needs the header just as much.
        """
        env = build_git_env(_config(github_token=TOKEN), HTTPS_URL)

        assert env["GIT_TERMINAL_PROMPT"] == "0"
        entries = {
            env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
            for index in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert entries[AUTH_KEY] == AUTH_VALUE

    def test_ssh_clone_sets_no_git_config_block(self) -> None:
        env = build_git_env(_config(github_token=TOKEN), "git@github.com:org/repo.git")

        assert "GIT_CONFIG_COUNT" not in env


class TestTrustedOrigin:
    """Scoping the credential says nothing about trusting the host."""

    def test_a_foreign_host_is_refused(self) -> None:
        """``project.clone_url`` is externally supplied.

        A URL pointing elsewhere would otherwise get a perfectly scoped
        header -- straight to the attacker.
        """
        with pytest.raises(UnsafeCloneUrlError, match="configured source"):
            resolve_clone_url(
                _project("https://attacker.example/org/repo.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_the_rejected_origin_is_not_named_in_the_error(self) -> None:
        """The refusal is logged, and the URL is the suspect value.

        Naming its host publishes externally supplied text, so a host
        of ``<token>.evil.example`` would leak the very secret the
        refusal exists to withhold.
        """
        hostile = f"https://{TOKEN}.evil.example/org/repo.git"

        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(
                _project(hostile),
                _config(use_https=True, github_token=TOKEN),
            )

        message = str(excinfo.value)
        assert TOKEN not in message
        assert "evil.example" not in message
        # The configured source is this run's own, and still named.
        assert "github.com" in message

    def test_the_configured_host_is_accepted(self) -> None:
        url = resolve_clone_url(
            _project(HTTPS_URL), _config(use_https=True, github_token=TOKEN)
        )

        assert url == HTTPS_URL

    def test_an_org_suffix_is_not_part_of_the_authority(self) -> None:
        """``config.host`` carries an org path that a URL host will not."""
        assert trusted_clone_origin(_config()) == ("github.com", 443)
        assert trusted_clone_origin(
            Config(host="https://ghe.example.com/ORG", source_type=SourceType.GITHUB)
        ) == ("ghe.example.com", 443)

    def test_a_different_port_is_a_different_origin(self) -> None:
        """A service on another port is not the one the token is for."""
        with pytest.raises(UnsafeCloneUrlError, match="configured"):
            resolve_clone_url(
                _project("https://github.com:8443/org/repo.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_an_explicit_zero_port_is_not_the_default(self) -> None:
        """Zero is falsy, and was quietly replaced by the scheme default.

        That made ``github.com:0`` compare equal to the trusted
        ``github.com:443`` and collect the token.
        """
        assert trusted_clone_origin(
            Config(host="https://github.com:0/ORG", source_type=SourceType.GITHUB)
        ) == ("github.com", 0)

        with pytest.raises(UnsafeCloneUrlError, match="configured"):
            resolve_clone_url(
                _project("https://github.com:0/org/repo.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_an_explicit_default_port_still_matches(self) -> None:
        url = resolve_clone_url(
            _project("https://github.com:443/org/repo.git"),
            _config(use_https=True, github_token=TOKEN),
        )

        assert url == "https://github.com:443/org/repo.git"

    def test_a_configured_enterprise_port_is_trusted(self) -> None:
        config = Config(
            host="https://ghe.example.com:8443/ORG",
            source_type=SourceType.GITHUB,
            use_https=True,
            github_token=TOKEN,
        )

        assert trusted_clone_origin(config) == ("ghe.example.com", 8443)
        assert (
            resolve_clone_url(_project("https://ghe.example.com:8443/o/r.git"), config)
            == "https://ghe.example.com:8443/o/r.git"
        )

    def test_a_bracketed_ipv6_host_is_not_truncated(self) -> None:
        """Splitting on the first colon would leave ``[`` as the host."""
        config = Config(
            host="https://[::1]/ORG",
            source_type=SourceType.GITHUB,
            use_https=True,
            github_token=TOKEN,
        )

        assert trusted_clone_origin(config) == ("::1", 443)
        assert (
            resolve_clone_url(_project("https://[::1]/o/r.git"), config)
            == "https://[::1]/o/r.git"
        )

    def test_a_foreign_host_is_allowed_without_a_token(self) -> None:
        """With nothing to leak, refusing would only break public clones."""
        url = resolve_clone_url(
            _project("https://gitlab.example/org/repo.git"), _config(use_https=True)
        )

        assert url == "https://gitlab.example/org/repo.git"

    def test_the_environment_refuses_a_foreign_host_too(self) -> None:
        """The header is built here, so the check belongs here as well."""
        with pytest.raises(UnsafeCloneUrlError):
            build_git_env(
                _config(use_https=True, github_token=TOKEN),
                "https://attacker.example/org/repo.git",
            )


class TestRefusedCloneUrls:
    """A URL git would misread never reaches the command line."""

    def test_an_option_like_url_is_refused(self) -> None:
        """The URL is positional, so a leading ``-`` becomes an option.

        ``git clone --bundle-uri=...`` would fetch from wherever the
        attacker points rather than from a repository.  ``--`` in the
        command guards the git path; refusing the value guards any
        other consumer of it too.
        """
        with pytest.raises(UnsafeCloneUrlError, match="option"):
            resolve_clone_url(
                _project("--bundle-uri=https://attacker.example/bundle"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_an_option_like_ssh_override_is_refused_too(self) -> None:
        """The SSH branch returns an externally supplied URL as well."""
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="--upload-pack=evil:command",
        )

        with pytest.raises(UnsafeCloneUrlError, match="option"):
            resolve_clone_url(project, _config())

    def test_an_invalid_port_is_refused_not_raised_raw(self) -> None:
        """``urlparse`` accepts it; only ``.port`` complains, and late.

        A bare ValueError would escape the caller's UnsafeCloneUrlError
        handling and abort the clone with a traceback.
        """
        with pytest.raises(UnsafeCloneUrlError, match="malformed authority"):
            resolve_clone_url(
                _project("https://github.com:notaport/org/repo.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_a_malformed_clone_url_is_refused_not_raised_raw(self) -> None:
        """``urlparse`` raises outright for an unmatched IPv6 bracket.

        The trust check parses the clone URL itself before handing the
        authority on, and callers handle only UnsafeCloneUrlError, so a
        bare ValueError there aborts the whole run with a traceback
        instead of failing the one project.
        """
        with pytest.raises(UnsafeCloneUrlError, match="malformed authority"):
            resolve_clone_url(
                _project("https://[::1/org/repo.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_a_malformed_configured_host_is_refused_too(self) -> None:
        """``urlparse`` raises outright for an unmatched IPv6 bracket.

        The configured host goes through the same parsing, so it needs
        the same conversion.
        """
        config = Config(
            host="https://[::1/ORG",
            source_type=SourceType.GITHUB,
            use_https=True,
            github_token=TOKEN,
        )

        with pytest.raises(UnsafeCloneUrlError, match="malformed authority"):
            resolve_clone_url(_project(HTTPS_URL), config)
