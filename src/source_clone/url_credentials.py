# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Credentials in git URLs: where they hide, and keeping them out of output.

Git accepts URLs a credential can hide in: the userinfo of
``scheme://user:secret@host/path``, its query or fragment, the user of
the scp-like ``user@host:path``, and the address of a remote helper,
``<transport>::<address>``, which is itself any of these.

Two jobs need that grammar, and both use this module.  Refusing a clone
URL that carries a credential, before it reaches git, is
:func:`source_clone.github_url_safety.reject_credentialed_url`, which
checks a remote helper's address through :func:`helper_address`.
Keeping a credential out of everything the tool writes -- log lines,
error messages, manifests, the filter journal -- is :func:`redact_url`
for a URL and :func:`redact_text` for text that may quote one, such as
git's own output.  Nothing else strips credentials by hand.

The same grammar also tells where a remote points, without its
credential: :func:`remote_location` gives a URL's scheme, host, port
and path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

#: What a credential is replaced with in text.
REDACTED = "***"

#: What stands in for a URL whose shape cannot be told apart from one
#: hiding a credential.
UNRECOGNISED_URL = "<URL not shown>"

#: Git's remote-helper syntax, ``<transport>::<address>``: a name made
#: of URL-scheme characters, then ``::`` (see git's ``transport.c``).
_REMOTE_HELPER = re.compile(
    r"^(?P<transport>[A-Za-z][A-Za-z0-9+.-]*)::(?P<address>.*)$", re.DOTALL
)

#: RFC 3986's split of a hierarchical URL: an optional scheme, ``//``,
#: the authority, then the path.  Whatever follows, a query or a
#: fragment, is never kept.
_HIERARCHICAL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*:)?//(?P<authority>[^/?#]*)(?P<path>[^?#]*)",
    re.DOTALL,
)

#: Git's scp-like ``user@host:path``; the host a name or a bracketed
#: IPv6 address, and the user anything up to the ``@``, a token included.
_SCP_LIKE = re.compile(r"^[^/@]+@(?P<rest>(?:\[[^\]/]+\]|[^/:\[\]]+):.*)$", re.DOTALL)

#: The ``host:path`` of a scp-like URL, its user removed or absent.
_SCP_LOCATION = re.compile(
    r"^(?P<host>\[[^\]/]+\]|[^/:@\[\]]+):(?P<path>.*)$", re.DOTALL
)

#: An authority with its userinfo removed: a name or a bracketed IPv6
#: address, then an optional port.
_HOST_PORT = re.compile(
    r"^(?:\[(?P<ipv6>[^\]]*)\]|(?P<name>[^:\[\]]*))(?::(?P<port>\d*))?$"
)

#: Userinfo in a URL quoted inside other text.
_QUOTED_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@'\"<>]*@")


def helper_address(url: str) -> str:
    """The address a remote-helper URL hands its helper; *url* otherwise.

    Nested helpers are unwrapped too: what the innermost receives is
    what a credential would reach.
    """
    while helper := _REMOTE_HELPER.match(url):
        url = helper.group("address")
    return url


class RemoteLocation(NamedTuple):
    """Where a git remote URL points, without any credential.

    Attributes:
        scheme: The URL scheme, in lower case; ``ssh`` for scp-like.
        host: The host, in lower case, without brackets or a final dot.
        port: The port, when the URL names one.
        path: The path, as written.
    """

    scheme: str
    host: str
    port: int | None
    path: str


def remote_location(url: str) -> RemoteLocation | None:
    """Where *url* points, or None for a local path or an unknown shape.

    A remote helper's address is read in its place.  Both the
    hierarchical form and git's scp-like ``[user@]host:path`` are
    recognised; a query or a fragment is never part of the path.
    """
    url = helper_address(url)
    hierarchical = _HIERARCHICAL.match(url)
    if hierarchical:
        scheme = (hierarchical.group("scheme") or "").rstrip(":").lower()
        authority = hierarchical.group("authority").rpartition("@")[2]
        path = hierarchical.group("path")
    else:
        scp_like = _SCP_LIKE.match(url)
        location = _SCP_LOCATION.match(scp_like.group("rest") if scp_like else url)
        if not location:
            return None
        scheme, authority, path = "ssh", location.group("host"), location.group("path")
    host_port = _HOST_PORT.match(authority)
    if not host_port:
        return None
    host = host_port.group("ipv6") or host_port.group("name") or ""
    port = host_port.group("port")
    return RemoteLocation(
        scheme=scheme,
        host=host.lower().rstrip("."),
        port=int(port) if port else None,
        path=path,
    )


def redact_url(url: str) -> str:
    """*url* with every part that can carry a credential taken out.

    For recording a URL, never for using one.  A hierarchical URL loses
    its userinfo, query and fragment; a scp-like one its user; a remote
    helper's address is redacted in turn.  A local path, or a bare name,
    is returned as it is.  Anything else that still holds an ``@`` or a
    ``://`` gives :data:`UNRECOGNISED_URL`, since what part of it is a
    credential cannot be told.
    """
    helper = _REMOTE_HELPER.match(url)
    if helper:
        return f"{helper.group('transport')}::{redact_url(helper.group('address'))}"
    hierarchical = _HIERARCHICAL.match(url)
    if hierarchical:
        scheme = hierarchical.group("scheme") or ""
        host = hierarchical.group("authority").rpartition("@")[2]
        return f"{scheme}//{host}{hierarchical.group('path')}"
    scp_like = _SCP_LIKE.match(url)
    if scp_like:
        return scp_like.group("rest")
    if "@" in url or "://" in url:
        return UNRECOGNISED_URL
    return url


def redact_text(text: str, secrets: Iterable[str | None] = ()) -> str:
    """*text* with each of *secrets*, and any quoted URL's userinfo, hidden.

    For text the tool did not write itself, such as git's output, which
    can quote the URL it used.  Each secret given, such as a configured
    token, becomes :data:`REDACTED` wherever it appears; so does the
    userinfo of every ``scheme://user:secret@`` in it.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return _QUOTED_USERINFO.sub(rf"\g<scheme>{REDACTED}@", text)
