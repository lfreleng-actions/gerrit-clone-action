# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether a clone URL is safe to hand to git.

Two refusals live here: a value git would read as an option rather
than a repository, and one carrying a credential of its own.  Both
apply to ``project.clone_url``, which is externally supplied, and both
run before git or ``gh`` is invoked.

The governing rule is that classification fails *open* and detection
fails *closed*.  An earlier attempt re-implemented git's URL grammar
on top of ``urlparse``, which implements a different one, and refused
repositories git clones perfectly well; nothing here tries to decide
what git would make of a URL it cannot positively identify a
credential in.

Which URL to clone from, and the origin and redaction checks around
it, are in :mod:`gerrit_clone.github_clone_url`.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse


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


#: Schemes in which a bare username is itself the credential.  GitHub
#: takes a token that way (``https://TOKEN@github.com/...``), so for
#: these any userinfo at all is refused.  Elsewhere a username is just
#: a username -- ``ssh://git@github.com/...`` is the ordinary form --
#: and only a password is positively a credential.
_USERNAME_IS_CREDENTIAL = frozenset({"http", "https"})


#: What ``urlparse`` discards before it parses: tab and newlines
#: anywhere, and C0 controls or space at the *leading* edge only.
#: Trailing ones are kept -- ``https://h/r.git# `` parses to a fragment
#: of ``" "`` -- so stripping both ends would erase components the
#: parse can see.  The textual fallback has to read the same value, or
#: it reads a different scheme, `` https`` rather than ``https``, and
#: the HTTP(S)-only rules never fire.
_DISCARDED_ANYWHERE = ("\t", "\r", "\n")
_DISCARDED_AT_START = "".join(chr(point) for point in range(0x21))


def _as_urlparse_sees_it(url: str) -> str:
    """*url* with what ``urlparse`` would discard already gone.

    Args:
        url: Raw value.

    Returns:
        The value parsing actually operates on.
    """
    for discarded in _DISCARDED_ANYWHERE:
        url = url.replace(discarded, "")
    return url.lstrip(_DISCARDED_AT_START)


def _component_after(text: str, separator: str) -> bool:
    """Whether *text* has content following its first *separator*.

    ``urlparse`` reports a component only when something follows its
    separator: ``repo.git;`` has no parameters and ``repo.git?`` no
    query.  Testing for the separator alone would make the textual
    path stricter than the parse, which is the opposite of what it is
    for.

    Args:
        text: Portion of the URL to examine.
        separator: Character introducing the component.

    Returns:
        Whether the component is present and non-empty.
    """
    _, marker, component = text.partition(separator)
    return bool(marker and component)


def _reject_opaque_components(*, params: bool, query: bool, fragment: bool) -> None:
    """Refuse parts of a URL that cannot be inspected for a credential.

    A credential can hide in any of them, and naming the parameters
    that might hold one is the guesswork that sank the first attempt.
    So rather than guess, this declines to vouch for content it cannot
    read.  Clone URLs here come from a GitHub listing, which carries
    none of them, so nothing git would be asked to clone is lost.

    Both paths call this rather than repeating the rules, which is how
    they last diverged.

    Args:
        params: Whether the last path segment carries ``;`` parameters.
        query: Whether the URL carries a query string.
        fragment: Whether it carries a non-empty fragment.

    Raises:
        UnsafeCloneUrlError: If any is present.
    """
    for present, name in (
        (params, "path parameters"),
        (query, "a query string"),
        (fragment, "a fragment"),
    ):
        if present:
            raise UnsafeCloneUrlError(
                f"Clone URL carries {name}, which cannot be shown to be "
                f"credential-free before it reaches the git command line"
            )


def _reject_credential_in_raw_url(url: str) -> None:
    """Apply the same rules textually to a URL ``urlparse`` refused.

    Failing open on an unparsable URL is right for *classification* --
    the origin check owns that case -- but not when a credential is
    plainly visible regardless: ``https://user:secret@[unclosed/r.git``
    raises while parsing, and passing it through hands the secret to
    git all the same.  A query carried by such a URL is the same story.

    These are the rules from :func:`reject_credentialed_url`, not a
    second set, read off the parts of RFC 3986 that stay unambiguous
    when the rest does not.

    Args:
        url: Value ``urlparse`` refused, containing ``://``.

    Raises:
        UnsafeCloneUrlError: If a credential is identifiable in it.
    """
    scheme, _, remainder = url.partition("://")
    over_http = scheme.lower() in _USERNAME_IS_CREDENTIAL

    authority = remainder
    for delimiter in ("/", "?", "#"):
        authority = authority.split(delimiter, 1)[0]

    if "@" in authority:
        # Userinfo runs to the *last* ``@``; a later one would be in
        # the host, which is invalid anyway.
        userinfo = authority.rsplit("@", 1)[0]
        if ":" in userinfo:
            raise UnsafeCloneUrlError(
                "Clone URL carries a password, which would reach the git command line"
            )
        if userinfo and over_http:
            raise UnsafeCloneUrlError(
                "Clone URL carries a credential in its userinfo, which "
                "would reach the git command line"
            )

    if over_http:
        # Read the way ``urlparse`` reads it: the fragment starts at
        # the first ``#``, the query at the first ``?`` before that,
        # and ``;`` parameters count only in the last path segment.
        rest = remainder[len(authority) :]
        before_fragment, marker, fragment = rest.partition("#")
        path = before_fragment.split("?", 1)[0]
        _reject_opaque_components(
            params=_component_after(path.rsplit("/", 1)[-1], ";"),
            query=_component_after(before_fragment, "?"),
            fragment=bool(marker and fragment),
        )


def _carries_token(url: str, token: str) -> bool:
    """Whether *token* is in *url* under any representation git resolves.

    Searching the literal value alone is not enough.  Discarding a
    newline can rejoin a token split across one, and percent-encoding
    hides it from a plain search while git decodes it right back --
    so ``%67hp_...`` would reach ``argv`` as the secret itself.

    Args:
        url: Clone URL being examined.
        token: This run's configured token.

    Returns:
        Whether any representation contains it.
    """
    normalised = _as_urlparse_sees_it(url)
    return any(
        token in candidate
        for candidate in (url, normalised, unquote(url), unquote(normalised))
    )


def reject_credentialed_url(url: str, token: str | None = None) -> None:
    """Refuse a clone URL that carries a credential of its own.

    ``project.clone_url`` is externally supplied.  This tool never adds
    a credential to it, but one already present would reach ``argv``
    and so the host's process listing, whatever its origin.

    An earlier attempt re-implemented git's URL grammar on top of
    ``urlparse``, which implements a different one, and spent most of
    its review on the corner cases -- refusing, among others, a
    ``github.com:org/repo.git`` that git clones perfectly well.  So
    only what ``urlparse`` is specified for is examined, and only what
    can be identified without guessing:

    - The **configured token**, wherever it appears and under any
      representation git resolves -- percent-encoded, or split by a
      character the parse discards.  The value is known, so finding it
      is identification, not classification.
    - A **password** under any scheme, which covers ``ssh://`` and its
      ``git+ssh`` aliases without naming them.
    - A **username** over HTTP(S) only, where it is how a token is
      passed.  Refusing it generally would reject
      ``ssh://git@github.com/...``, which git clones every day.
    - **Path parameters, a query string or a fragment** over HTTP(S).
      A credential can hide in any of them, and naming the parameters
      that might hold it is the guesswork that sank the first attempt,
      so this declines to vouch for content it cannot inspect.  Clone
      URLs here come from a GitHub listing, which carries none.
    - Anything without ``://`` is passed through: scp-style or unknown,
      with no userinfo syntax, and the shape the grammars disagree on.
    - A URL ``urlparse`` cannot split is examined textually instead,
      by :func:`_reject_credential_in_raw_url`.

    Classification therefore fails *open* and detection fails *closed*:
    a shape this cannot recognise is left alone, and a credential it
    does recognise ends the clone.

    Args:
        url: Clone URL about to be handed to git.
        token: This run's configured token, if any, so that a URL
            carrying it can be recognised outright.

    Raises:
        UnsafeCloneUrlError: If the URL carries a credential.  The
            message never repeats the value, being logged and reported.
    """
    if token and _carries_token(url, token):
        raise UnsafeCloneUrlError(
            "Clone URL contains the configured token, which would reach "
            "the git command line"
        )

    # Everything below reads the value parsing operates on, so that the
    # structural and textual paths cannot disagree about the scheme.
    url = _as_urlparse_sees_it(url)

    if "://" not in url:
        return

    try:
        parsed = urlparse(url)
        username, password = parsed.username, parsed.password
    except ValueError:
        _reject_credential_in_raw_url(url)
        return

    if password is not None:
        # Presence, not truthiness: ``ssh://git:@host/r.git`` parses to
        # an empty password, which the textual path catches as a ``:``
        # in the userinfo.  Testing for truth would leave the two
        # paths disagreeing about the same rule.
        raise UnsafeCloneUrlError(
            "Clone URL carries a password, which would reach the git command line"
        )

    scheme = parsed.scheme.lower()
    if username and scheme in _USERNAME_IS_CREDENTIAL:
        raise UnsafeCloneUrlError(
            "Clone URL carries a credential in its userinfo, which would "
            "reach the git command line"
        )

    if scheme in _USERNAME_IS_CREDENTIAL:
        _reject_opaque_components(
            params=bool(parsed.params),
            query=bool(parsed.query),
            fragment=bool(parsed.fragment),
        )
