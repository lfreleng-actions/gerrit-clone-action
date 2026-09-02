# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Selection and redaction of the URL used to clone a GitHub repository.

Chooses between SSH and HTTPS, and produces a redacted variant for
logging.  Nothing here adds a credential to the URL: HTTPS token
authentication travels in the process environment instead, through
:mod:`gerrit_clone.github_clone_env`.

That is narrower than the URL being credential-free.
``project.clone_url`` is externally supplied and is returned as given,
so a credential already in it still reaches ``argv``; removing that is
deferred to issue #277.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import ParseResult, urlparse, urlunparse

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from gerrit_clone.models import Config, Project

logger = get_logger(__name__)


class UnsafeCloneUrlError(ValueError):
    """Raised when a clone URL must not be handed to git.

    Deliberately carries no URL text, since the value that provoked it
    is the one under suspicion.
    """


def _reject_option_like_url(url: str) -> None:
    """Refuse a value ``git`` would read as an option rather than a URL.

    ``project.clone_url`` is externally supplied and is passed
    positionally, so a value beginning with ``-`` would be taken for an
    option -- ``--upload-pack=...`` and friends.  ``git clone`` is given
    a ``--`` terminator as well, in
    :func:`gerrit_clone.clone_utils.build_base_clone_command`; this is
    the other half of that pair.

    Args:
        url: Clone URL about to be used.

    Raises:
        UnsafeCloneUrlError: If git would read the value as an option.
    """
    if url.startswith("-"):
        raise UnsafeCloneUrlError(
            "Clone URL begins with '-', so git would read it as an option"
        )


def resolve_clone_url(project: Project, config: Config) -> str:
    """Determine the URL to clone from - prefer SSH, fall back to HTTPS.

    The configured token is never added.  It used to be embedded here,
    which put it in the ``git clone`` arguments and so in the host's
    process listing; authentication now travels in the environment
    instead (see :func:`gerrit_clone.github_clone_env.build_git_env`).

    The result is not credential-free in general, only free of anything
    this tool put there: ``project.clone_url`` is externally supplied
    and is returned as given, so a credential already in it still
    reaches ``argv``.  Stripping that is deferred to issue #277.

    Args:
        project: Project to clone
        config: Configuration with optional github_token

    Returns:
        The clone URL, with no credential added by this tool.

    Raises:
        UnsafeCloneUrlError: If the URL is not one git would read as a
            repository, or is not on the configured source host.
    """
    if config.use_https:
        # Explicit HTTPS requested
        clone_url = project.clone_url or project.https_url(config.base_url)
        _reject_option_like_url(clone_url)
        assert_trusted_origin(clone_url, config)

        if config.github_token and clone_url.startswith("https://"):
            logger.debug(
                f"Cloning {project.name} with HTTPS using token authentication"
            )
        else:
            logger.debug(
                f"Cloning {project.name} with HTTPS (no token, will use credential helper)"
            )
        return clone_url

    if project.ssh_url_override:
        # SSH URL available from GitHub (preferred)
        _reject_option_like_url(project.ssh_url_override)
        return project.ssh_url_override

    # Fall back to HTTPS if no SSH URL available
    clone_url = project.clone_url or project.https_url(config.base_url)
    _reject_option_like_url(clone_url)
    assert_trusted_origin(clone_url, config)
    return clone_url


#: Default port for each scheme the trust check understands, so an
#: explicit ``:443`` and an implicit one compare equal.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _parsed_or_refused(url: str) -> ParseResult:
    """Parse *url*, turning ``urlparse``'s own refusal into ours.

    ``urlparse`` raises for a malformed authority -- an unmatched IPv6
    bracket, say -- and every caller here handles only
    :class:`UnsafeCloneUrlError`, so a bare ``ValueError`` would escape
    to abort the run with a traceback rather than failing that one
    clone.

    Args:
        url: Value to parse.

    Returns:
        The parsed URL.

    Raises:
        UnsafeCloneUrlError: If it cannot be parsed at all.
    """
    try:
        return urlparse(url)
    except ValueError as exc:
        raise UnsafeCloneUrlError(
            "Clone URL has a malformed authority, so its origin cannot be identified"
        ) from exc


def _origin_of(scheme: str, authority: str) -> tuple[str, int | None]:
    """Split *authority* into a lowercase host and an effective port.

    Parsed rather than split on ``:``, which would truncate a bracketed
    IPv6 authority such as ``[::1]:8443`` at its first colon.

    Raises:
        UnsafeCloneUrlError: If the authority cannot be parsed, or its
            port is not a number.  ``urlparse`` accepts a bad port and
            complains only on ``.port``, so that has to be converted
            here as well or it escapes the caller's handling.
    """
    parsed = _parsed_or_refused(f"{scheme}://{authority}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeCloneUrlError(
            "Clone URL has a malformed authority, so its origin cannot be identified"
        ) from exc
    # Only an absent port falls back to the scheme default: ``:0`` is
    # explicit, and every explicit port is its own origin.
    if port is None:
        port = _DEFAULT_PORTS.get(scheme.lower())
    return ((parsed.hostname or "").lower(), port)


def trusted_clone_origin(config: Config) -> tuple[str, int | None]:
    """Return the host and port a clone URL must match.

    ``config.host`` may carry a scheme and an org suffix
    (``https://github.com/ORG``); neither is part of the authority.  An
    explicit port is kept, so a GitHub Enterprise install on a
    non-standard port is trusted on that port and no other.

    Args:
        config: Configuration naming the source host.

    Returns:
        Lowercase hostname and effective port.
    """
    authority = config.host
    scheme = "https"
    if "://" in authority:
        scheme, authority = authority.split("://", 1)
    return _origin_of(scheme, authority.split("/", 1)[0])


def assert_trusted_origin(url: str, config: Config) -> None:
    """Refuse an HTTP(S) *url* that is not on the configured origin.

    Scoping the credential to the URL's own origin stops it reaching
    anywhere *else*, but says nothing about whether that origin should
    be trusted at all.  ``project.clone_url`` is externally supplied, so
    a URL pointing at another host would otherwise be handed the token
    quite correctly scoped -- straight to the attacker.

    The port is part of the comparison: a different port on the same
    host is a different origin, and a service listening there is not the
    one the token was issued for.

    Only checked when a token is configured: without one there is
    nothing to leak, and refusing would break unauthenticated clones
    for no gain.

    Args:
        url: Clone URL about to be used.  Externally supplied and not
            sanitised: this checks where the URL points, and nothing
            about what it may carry.
        config: Configuration naming the source host and token.

    Raises:
        UnsafeCloneUrlError: If the URL's origin is not the configured
            one.
    """
    if not config.github_token or not url.startswith("https://"):
        # Only an https:// URL can receive the header (see
        # ``build_git_env``), so only that needs the host checked.
        # Refusing a plaintext http:// clone here would break one that
        # never had the token offered to it in the first place.
        return

    parsed = _parsed_or_refused(url)
    actual = _origin_of(parsed.scheme, parsed.netloc)
    expected = trusted_clone_origin(config)
    if actual != expected:
        # The rejected origin is deliberately not named.  This message
        # is logged, and it describes a URL that is externally supplied
        # and already under suspicion -- a host of
        # ``<token>.evil.example`` would otherwise publish the very
        # secret being withheld.  Only the configured source is named,
        # which comes from this run's own configuration, and the caller
        # names the project, which is what locating it actually takes.
        raise UnsafeCloneUrlError(
            f"Clone URL is not on the configured source "
            f"{expected[0]!r}:{expected[1]}, so it must not receive the token"
        )


def redact_clone_url(clone_url: str, project: Project, github_token: str | None) -> str:
    """Return *clone_url* with any embedded token replaced by ``***``.

    The URL is parsed and reconstructed rather than string-replaced to
    avoid issues with special characters in the token.

    Args:
        clone_url: URL that may contain embedded credentials
        project: Project being cloned, used for the safe placeholder
        github_token: Token to look for, if one is configured

    Returns:
        A URL that is safe to log.
    """
    if not github_token:
        return clone_url

    try:
        parsed = urlparse(clone_url)
        # Check if token is in the netloc (e.g., token@github.com)
        if "@" in parsed.netloc and github_token in parsed.netloc:
            # Reconstruct netloc with redacted token
            netloc_parts = parsed.netloc.split("@", 1)
            redacted_netloc = f"***@{netloc_parts[1]}"
            return urlunparse(
                (
                    parsed.scheme,
                    redacted_netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
    except Exception:
        # SECURITY: If parsing fails, use safe placeholder to avoid credential leak
        return f"https://***@github.com/{project.name}.git"

    return clone_url
