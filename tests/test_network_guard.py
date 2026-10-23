# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The unit suite stays off the network, and says so when it would not.

A Gerrit ``Config`` discovers its API base URL over HTTPS.  On a runner
that drops outbound connections rather than refusing them, each one
waited out a connect timeout per path tried, and the suite ran past the
job's time limit.  ``conftest.no_network`` stops that; these tests keep
it doing so.
"""

from __future__ import annotations

import contextlib
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from source_clone.discovery import GerritAPIDiscovery
from source_clone.models import Config


def _clear(attempts: list[str]) -> None:
    """Forget the attempts a test made on purpose, so teardown passes."""
    attempts.clear()


def test_a_gerrit_config_falls_back_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No discovery attempted, and the URL a failed one gives.

    Counted, not timed: where DNS fails fast, live discovery is quick too.
    """
    attempted: list[str] = []
    real = GerritAPIDiscovery.discover_base_url

    def counting(self: GerritAPIDiscovery, host: str) -> str:
        attempted.append(host)
        found: str = real(self, host)
        return found

    monkeypatch.setattr(GerritAPIDiscovery, "discover_base_url", counting)

    config = Config(host="gerrit.example.org")

    assert config.base_url == "https://gerrit.example.org"
    assert attempted == []


def test_resolving_a_name_is_refused(no_network: list[str]) -> None:
    with pytest.raises(RuntimeError, match="tried to reach the network"):
        socket.getaddrinfo("gerrit.example.org", 443)

    assert no_network == ["getaddrinfo gerrit.example.org"]
    _clear(no_network)


def test_a_name_starting_like_loopback_is_refused(no_network: list[str]) -> None:
    """A name, not an address: it resolves anywhere on the network."""
    with pytest.raises(RuntimeError, match="tried to reach the network"):
        socket.getaddrinfo("127.0.0.1.example.org", 443)

    assert no_network == ["getaddrinfo 127.0.0.1.example.org"]
    _clear(no_network)


@pytest.mark.parametrize(
    ("lookup", "arguments"),
    [
        (socket.gethostbyname, ("gerrit.example.org",)),
        (socket.gethostbyname_ex, ("gerrit.example.org",)),
        (socket.gethostbyaddr, ("192.0.2.1",)),
    ],
    ids=["gethostbyname", "gethostbyname_ex", "gethostbyaddr"],
)
def test_every_resolver_entry_point_is_refused(
    no_network: list[str], lookup: Any, arguments: tuple[str, ...]
) -> None:
    """Each calls the system resolver itself, not through getaddrinfo."""
    guarded = getattr(socket, lookup.__name__)

    with pytest.raises(RuntimeError, match="tried to reach the network"):
        guarded(*arguments)

    assert no_network == [f"{lookup.__name__} {arguments[0]}"]
    _clear(no_network)


def test_connecting_off_this_machine_is_refused(no_network: list[str]) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="tried to reach the network"),
    ):
        sock.connect(("192.0.2.1", 443))

    assert no_network == ["connect ('192.0.2.1', 443)"]
    _clear(no_network)


def test_loopback_is_allowed(no_network: list[str]) -> None:
    """Tests that serve on this machine keep working."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        # Bounded, so a guard that wrongly refuses loopback fails this test
        # rather than leaving the thread blocked and pytest unable to exit.
        server.settimeout(5)

        def accept() -> None:
            with contextlib.suppress(OSError):
                server.accept()[0].close()

        accepted = threading.Thread(target=accept, daemon=True)
        accepted.start()
        try:
            with socket.create_connection(server.getsockname(), timeout=5):
                pass
        finally:
            accepted.join(timeout=6)

    assert no_network == []


def test_a_datagram_off_this_machine_is_refused(no_network: list[str]) -> None:
    """sendto needs no connect, and no lookup for a numeric address."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(RuntimeError, match="tried to reach the network"),
    ):
        sock.sendto(b"x", ("192.0.2.1", 53))

    assert no_network == ["sendto ('192.0.2.1', 53)"]
    _clear(no_network)


@pytest.mark.skipif(
    not hasattr(socket.socket, "sendmsg"), reason="no sendmsg on this platform"
)
def test_a_message_off_this_machine_is_refused(no_network: list[str]) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(RuntimeError, match="tried to reach the network"),
    ):
        sock.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))

    assert no_network == ["sendmsg ('192.0.2.1', 53)"]
    _clear(no_network)


def test_a_datagram_to_loopback_is_allowed(no_network: list[str]) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
    ):
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.sendto(b"x", receiver.getsockname())
        assert receiver.recv(1) == b"x"
        if hasattr(sender, "sendmsg"):
            sender.sendmsg([b"y"], [], 0, receiver.getsockname())
            assert receiver.recv(1) == b"y"
    assert no_network == []


def test_a_reverse_lookup_off_this_machine_is_refused(
    no_network: list[str],
) -> None:
    """getnameinfo resolves an address tuple, and needs its own guard."""
    with pytest.raises(RuntimeError, match="tried to reach the network"):
        socket.getnameinfo(("192.0.2.1", 443), socket.NI_NAMEREQD)

    assert no_network == ["getnameinfo ('192.0.2.1', 443)"]
    _clear(no_network)


def test_a_reverse_lookup_of_loopback_is_allowed(no_network: list[str]) -> None:
    host, _ = socket.getnameinfo(("127.0.0.1", 0), socket.NI_NUMERICHOST)

    assert host == "127.0.0.1"
    assert no_network == []


def test_a_loopback_address_is_named_without_the_resolver(
    no_network: list[str],
) -> None:
    """No hosts entry names 127.0.0.2, and the system resolver could ask
    DNS about it below Python's socket layer, unrecorded.  Answered here,
    both lookups name this machine; the resolver would have failed."""
    assert socket.gethostbyaddr("127.0.0.2")[0] == "localhost"
    assert socket.getnameinfo(("127.0.0.2", 443), socket.NI_NAMEREQD) == (
        "localhost",
        "443",
    )
    assert no_network == []


def test_a_name_with_a_scope_suffix_is_refused(no_network: list[str]) -> None:
    """Only an IPv6 address carries a scope; on an IPv4 one it makes a name."""
    with pytest.raises(RuntimeError, match="tried to reach the network"):
        socket.getaddrinfo("127.0.0.1%example.org", 443)

    assert no_network == ["getaddrinfo 127.0.0.1%example.org"]
    _clear(no_network)


def test_binding_to_a_host_name_is_refused(no_network: list[str]) -> None:
    """bind resolves a name itself, past the guarded lookups."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="tried to reach the network"),
    ):
        sock.bind(("gerrit.example.org", 0))

    assert no_network == ["bind ('gerrit.example.org', 0)"]
    _clear(no_network)


def test_binding_locally_is_allowed(no_network: list[str]) -> None:
    for host in ("127.0.0.1", "localhost", "", "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
    if hasattr(socket, "AF_UNIX"):
        # A short directory: a socket path has a small length limit, which
        # a deep temporary directory can exceed.  /tmp where there is one;
        # elsewhere, Windows included, the platform's own.
        short_parent = "/tmp" if Path("/tmp").is_dir() else None
        with (
            tempfile.TemporaryDirectory(dir=short_parent) as short,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock,
        ):
            sock.bind(str(Path(short) / "s"))
    assert no_network == []


def test_any_loopback_address_is_allowed(no_network: list[str]) -> None:
    """All of 127.0.0.0/8, and IPv6 loopback, stay on this machine."""
    assert socket.getaddrinfo("127.0.0.2", 0, socket.AF_INET, socket.SOCK_STREAM)
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert no_network == []


def test_binding_on_every_interface_is_allowed(no_network: list[str]) -> None:
    """A server's own lookup of the wildcard host stays on this machine."""
    assert socket.getaddrinfo(None, 0, socket.AF_INET, socket.SOCK_STREAM)
    assert socket.getaddrinfo("0.0.0.0", 0, socket.AF_INET, socket.SOCK_STREAM)
    assert no_network == []
