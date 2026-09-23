# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the shared git credential environment.

The clone path and the mirror push path must authenticate the same way:
through ``GIT_CONFIG_*`` environment variables, never through a token
embedded in a URL that ``git`` then receives as an argument.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gerrit_clone.git_credential_env import (
    build_token_auth_env,
    git_config_env,
    token_auth_config,
)
from gerrit_clone.github_clone_env import build_git_env
from gerrit_clone.github_clone_url import (
    resolve_clone_url,
    trusted_clone_origin,
)
from gerrit_clone.github_url_safety import UnsafeCloneUrlError
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


def _config(**kwargs: Any) -> Config:
    return Config(
        host="github.com/org",
        source_type=SourceType.GITHUB,
        **kwargs,
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

    def test_a_token_in_a_hostile_host_is_refused_without_leaking_it(
        self,
    ) -> None:
        """A host of ``<token>.evil.example`` carries the secret itself.

        The token rule now catches this before the origin check, which
        is the stricter of the two: the value never reaches the origin
        comparison at all.  What must hold either way is that the
        refusal -- which is logged and reported -- does not republish
        the secret it exists to withhold.
        """
        hostile = f"https://{TOKEN}.evil.example/org/repo.git"
        config = _config(use_https=True, github_token=TOKEN)

        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(_project(hostile), config)

        message = str(excinfo.value)
        assert TOKEN not in message
        assert "evil.example" not in message

    def test_the_rejected_origin_is_not_named_in_the_error(self) -> None:
        """The refusal is logged, and the URL is the suspect value.

        Naming its host publishes externally supplied text, so the
        message names this run's own configured source instead.
        """
        hostile = "https://evil.example/org/repo.git"
        config = _config(use_https=True, github_token=TOKEN)

        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(_project(hostile), config)

        message = str(excinfo.value)
        assert "evil.example" not in message
        # The configured source is this run's own, and still named.
        expected_host, _expected_port = trusted_clone_origin(config)
        assert expected_host in message

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


class TestSuppliedCredentialsAreRefused:
    """A credential in the supplied URL is a credential in ``argv``.

    ``project.clone_url`` comes from the GitHub listing for the
    configured org, so it is externally supplied.  This tool never adds
    a credential to it; one that arrives already there is refused
    rather than handed to git.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://token@github.com/org/repo.git",
            "https://user:pass@github.com/org/repo.git",
            "https://:pass@github.com/org/repo.git",
            "https://user:@github.com/org/repo.git",
            "http://token@github.com/org/repo.git",
        ],
        ids=[
            "user-only",
            "user-and-password",
            "password-only",
            "empty-password",
            "plaintext",
        ],
    )
    def test_userinfo_is_refused(self, url: str) -> None:
        with pytest.raises(UnsafeCloneUrlError, match="reach the git command line"):
            resolve_clone_url(
                _project(url), _config(use_https=True, github_token=TOKEN)
            )

    @pytest.mark.parametrize(
        "url",
        [
            " https://token@[unclosed/org/repo.git",
            "ht\ntps://token@[unclosed/org/repo.git",
            "\thttps://token@[unclosed/org/repo.git",
        ],
        ids=["leading-space", "embedded-newline", "leading-tab"],
    )
    def test_an_obfuscated_scheme_does_not_evade_the_http_rules(self, url: str) -> None:
        """The fallback must read what ``urlparse`` reads.

        ``urlparse`` discards tabs and newlines anywhere, and C0
        controls or space at the leading edge -- trailing ones are kept
        -- *before* parsing, so it sees ``https`` where a textual split
        sees `` https``.  Reading the raw value left the HTTP(S)-only
        rules unfired, and a bare userinfo went through to ``argv``.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override=url,
        )

        with pytest.raises(UnsafeCloneUrlError, match="reach the git command line"):
            resolve_clone_url(project, _config())

    def test_an_obfuscated_scheme_does_not_evade_the_query_rule(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override=" https://[unclosed/repo.git?access_token=secret",
        )

        with pytest.raises(UnsafeCloneUrlError, match="query string"):
            resolve_clone_url(project, _config())

    def test_a_token_split_by_a_discarded_newline_is_found(self) -> None:
        """Removing the newline brings the halves back together.

        The configured value is checked against both the raw URL and
        the one parsing sees, since either may be where it is whole.
        """
        split = f"https://github.com/org/{TOKEN[:4]}\n{TOKEN[4:]}.git"

        with pytest.raises(UnsafeCloneUrlError, match="configured token"):
            resolve_clone_url(
                _project(split), _config(use_https=True, github_token=TOKEN)
            )

    def test_an_empty_password_component_is_still_refused(self) -> None:
        """``ssh://git:@host`` parses to a password of ``""``.

        Testing it for truth rather than presence would let this form
        through, while the textual path catches it as a ``:`` in the
        userinfo -- leaving the two paths disagreeing about the same
        rule.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git:@github.com/org/repo.git",
        )

        with pytest.raises(UnsafeCloneUrlError, match="password"):
            resolve_clone_url(project, _config())

    def test_the_refusal_does_not_repeat_the_credential(self) -> None:
        """The message is logged and reported, so it must not carry it."""
        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(
                _project("https://hunter2@github.com/org/repo.git"),
                _config(use_https=True),
            )

        assert "hunter2" not in str(excinfo.value)

    def test_a_credentialed_ssh_override_is_refused_too(self) -> None:
        """A password is a credential under any scheme.

        ``ssh`` ignores it, but git still puts it in ``argv``.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git:secret@github.com/org/repo.git",
        )

        with pytest.raises(UnsafeCloneUrlError, match="password"):
            resolve_clone_url(project, _config())

    def test_a_password_is_refused_under_a_git_ssh_alias(self) -> None:
        """The alias schemes come free: the rule is on the component."""
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="git+ssh://git:secret@github.com/org/repo.git",
        )

        with pytest.raises(UnsafeCloneUrlError, match="password"):
            resolve_clone_url(project, _config())


class TestUnrecognisedShapesArePassedThrough:
    """Classification fails open; only detection fails closed.

    An earlier attempt re-implemented git's URL grammar on top of
    ``urlparse``, which implements a different one, and refused
    repositories git clones perfectly well.  Anything this cannot
    positively identify a credential in is left alone.
    """

    def test_scp_style_is_not_classified(self) -> None:
        """``urlparse`` reads the host as a scheme; git reads scp-style.

        This is the shape the first attempt refused.  It has no
        userinfo syntax after the colon, so there is nothing to detect.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="github.com:org/repo.git",
        )

        assert resolve_clone_url(project, _config()) == "github.com:org/repo.git"

    def test_scp_style_with_a_user_is_not_refused(self) -> None:
        """``git@host:path`` is a username, not a credential."""
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="git@github.com:org/repo.git",
        )

        assert resolve_clone_url(project, _config()) == "git@github.com:org/repo.git"

    def test_an_ssh_username_is_not_a_credential(self) -> None:
        """``ssh://git@github.com/...`` is the ordinary form.

        Refusing userinfo generally would reject it, which is the
        failure mode that sank the first attempt.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git@github.com/org/repo.git",
        )

        assert (
            resolve_clone_url(project, _config()) == "ssh://git@github.com/org/repo.git"
        )

    def test_a_non_http_scheme_is_passed_through(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="git://github.com/org/repo.git",
        )

        assert resolve_clone_url(project, _config()) == "git://github.com/org/repo.git"

    def test_a_query_string_is_refused(self) -> None:
        """A credential can hide in one, and which parameter is guesswork.

        Rather than name parameters -- the guesswork the first attempt
        died of -- this declines to vouch for a shape it cannot
        inspect.  Clone URLs here come from a GitHub listing, which
        never carries a query.
        """
        with pytest.raises(UnsafeCloneUrlError, match="query string"):
            resolve_clone_url(
                _project("https://github.com/org/repo.git?access_token=secret"),
                _config(use_https=True),
            )

    def test_a_query_refusal_does_not_repeat_the_value(self) -> None:
        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(
                _project("https://github.com/org/repo.git?access_token=hunter2"),
                _config(use_https=True),
            )

        assert "hunter2" not in str(excinfo.value)

    def test_a_query_is_left_alone_on_a_non_http_scheme(self) -> None:
        """The rule is scoped to the schemes whose URLs carry them."""
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git@github.com/org/repo.git?x=1",
        )

        assert (
            resolve_clone_url(project, _config())
            == "ssh://git@github.com/org/repo.git?x=1"
        )

    def test_userinfo_is_caught_even_when_parsing_fails(self) -> None:
        """Failing open on classification must not wave a credential past.

        ``urlparse`` raises on the malformed authority, but the
        credential is plainly visible in it regardless.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://user:secret@[unclosed/org/repo.git",
        )

        with pytest.raises(UnsafeCloneUrlError, match="password"):
            resolve_clone_url(project, _config())

    def test_a_bare_username_is_caught_when_parsing_fails_over_https(
        self,
    ) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://token@[unclosed/org/repo.git",
        )

        with pytest.raises(UnsafeCloneUrlError, match="reach the git command line"):
            resolve_clone_url(project, _config())

    def test_the_configured_token_is_refused_wherever_it_sits(self) -> None:
        """Structure says nothing about a value we already know.

        A token in the *path* is invisible to every rule above, but it
        needs no classification: the value is configured, so finding it
        is a positive identification rather than a guess.
        """
        with pytest.raises(UnsafeCloneUrlError, match="configured token"):
            resolve_clone_url(
                _project(f"https://github.com/org/{TOKEN}.git"),
                _config(use_https=True, github_token=TOKEN),
            )

    def test_the_token_refusal_does_not_repeat_it(self) -> None:
        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(
                _project(f"https://github.com/org/{TOKEN}.git"),
                _config(use_https=True, github_token=TOKEN),
            )

        assert TOKEN not in str(excinfo.value)

    def test_the_same_url_is_fine_without_that_token_configured(self) -> None:
        """The rule is about this run's own secret, not a shape."""
        url = f"https://github.com/org/{TOKEN}.git"

        assert resolve_clone_url(_project(url), _config(use_https=True)) == url

    def test_a_query_is_refused_even_when_parsing_fails(self) -> None:
        """A malformed authority must not carry a query past the rule.

        ``urlparse`` raises, and the authority holds no ``@``, so
        nothing would have stopped this before.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git?access_token=secret",
        )

        with pytest.raises(UnsafeCloneUrlError, match="query string"):
            resolve_clone_url(project, _config())

    def test_that_refusal_does_not_repeat_the_value(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git?access_token=hunter2",
        )

        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(project, _config())

        assert "hunter2" not in str(excinfo.value)

    def test_a_fragment_is_refused(self) -> None:
        """A fragment is as opaque as a query, and as capable of hiding one."""
        with pytest.raises(UnsafeCloneUrlError, match="fragment"):
            resolve_clone_url(
                _project("https://github.com/org/repo.git#access_token=secret"),
                _config(use_https=True),
            )

    def test_a_fragment_refusal_does_not_repeat_the_value(self) -> None:
        with pytest.raises(UnsafeCloneUrlError) as excinfo:
            resolve_clone_url(
                _project("https://github.com/org/repo.git#access_token=hunter2"),
                _config(use_https=True),
            )

        assert "hunter2" not in str(excinfo.value)

    def test_a_fragment_is_refused_when_parsing_fails(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git#access_token=secret",
        )

        with pytest.raises(UnsafeCloneUrlError, match="fragment"):
            resolve_clone_url(project, _config())

    def test_a_question_mark_inside_a_fragment_is_not_a_query(self) -> None:
        """The fragment starts at the first ``#``; a ``?`` after it is its own.

        Both are refused, so the distinction only shows in which rule
        reports it -- but reporting a fragment as a query would send a
        reader looking in the wrong place.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git#anchor?a=1",
        )

        with pytest.raises(UnsafeCloneUrlError, match="fragment"):
            resolve_clone_url(project, _config())

    def test_an_empty_fragment_is_not_a_credential(self) -> None:
        url = "https://github.com/org/repo.git#"

        assert resolve_clone_url(_project(url), _config(use_https=True)) == url

    def test_a_fragment_is_left_alone_on_a_non_http_scheme(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git@github.com/org/repo.git#frag",
        )

        assert (
            resolve_clone_url(project, _config())
            == "ssh://git@github.com/org/repo.git#frag"
        )

    def test_path_parameters_are_refused(self) -> None:
        """``;`` parameters are as opaque as a query or fragment."""
        with pytest.raises(UnsafeCloneUrlError, match="path parameters"):
            resolve_clone_url(
                _project("https://github.com/org/repo.git;access_token=secret"),
                _config(use_https=True),
            )

    def test_path_parameters_are_refused_when_parsing_fails(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git;access_token=secret",
        )

        with pytest.raises(UnsafeCloneUrlError, match="path parameters"):
            resolve_clone_url(project, _config())

    def test_a_semicolon_outside_the_last_segment_is_not_a_parameter(
        self,
    ) -> None:
        """``urlparse`` takes parameters from the last segment only.

        Refusing any ``;`` would part company with it, and reject a
        path git is perfectly happy with.
        """
        url = "https://github.com/org;group/repo.git"

        assert resolve_clone_url(_project(url), _config(use_https=True)) == url

    def test_path_parameters_are_left_alone_on_a_non_http_scheme(self) -> None:
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="ssh://git@github.com/org/repo.git;x=1",
        )

        assert (
            resolve_clone_url(project, _config())
            == "ssh://git@github.com/org/repo.git;x=1"
        )

    def test_a_trailing_whitespace_fragment_is_still_seen(self) -> None:
        """``urlparse`` keeps trailing whitespace; the fallback must too.

        ``https://h/r.git# `` parses to a fragment of ``" "``.  Stripping
        both ends when normalising erased it, so the component the
        parse can see went unexamined while the original -- trailing
        space and all -- was what reached git.
        """
        with pytest.raises(UnsafeCloneUrlError, match="fragment"):
            resolve_clone_url(
                _project("https://github.com/org/repo.git# "),
                _config(use_https=True),
            )

    def test_a_trailing_whitespace_query_is_still_seen(self) -> None:
        with pytest.raises(UnsafeCloneUrlError, match="query string"):
            resolve_clone_url(
                _project("https://github.com/org/repo.git?a=1 "),
                _config(use_https=True),
            )

    def test_a_percent_encoded_token_is_found(self) -> None:
        """git decodes it straight back, so a literal search is not enough.

        ``%67`` is ``g``, so the value reaches ``argv`` as the secret
        itself while a plain substring check sees nothing.
        """
        encoded = f"https://github.com/org/%67{TOKEN[1:]}.git"

        with pytest.raises(UnsafeCloneUrlError, match="configured token"):
            resolve_clone_url(
                _project(encoded), _config(use_https=True, github_token=TOKEN)
            )

    def test_a_bare_trailing_question_mark_is_not_a_query(self) -> None:
        """``urlparse("https://host/repo.git?").query`` is empty.

        The same rule as the bare ``;`` below: a component is what
        *follows* its separator, and refusing this in the fallback
        would be stricter than the parse.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git?",
        )

        assert resolve_clone_url(project, _config()) == "https://[unclosed/repo.git?"

    def test_a_bare_trailing_separator_is_not_a_parameter(self) -> None:
        """Parameters are what *follows* the first ``;``.

        ``urlparse("https://host/repo.git;").params`` is empty, so
        refusing this in the fallback would be stricter than the parse
        -- the opposite of what that path is for.
        """
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/repo.git;",
        )

        assert resolve_clone_url(project, _config()) == "https://[unclosed/repo.git;"

    def test_an_unparsable_authority_without_userinfo_is_passed_through(
        self,
    ) -> None:
        """Refusing here would be a classification failure, not a detection."""
        project = Project(
            name="org/repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            ssh_url_override="https://[unclosed/org/repo.git",
        )

        # No credential is identifiable, so this path does not refuse;
        # the SSH branch returns it and the origin check owns the rest.
        assert resolve_clone_url(project, _config()) == "https://[unclosed/org/repo.git"


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
