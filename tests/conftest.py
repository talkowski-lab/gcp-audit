"""Shared test fixtures.

`no_network` is autouse, so no test can reach the cloud. A test that needs an HTTP-shaped
interaction injects a fake transport instead.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*_a, **_k):
    raise NetworkBlocked(
        "tests are offline (AGENTS.md): install a fake session "
        "instead of reaching the network.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may open a connection, resolve a name, or mint application default credentials.

    This patches the connect methods rather than replacing the `socket.socket` class, because
    `ssl.SSLSocket` subclasses it. The class stays intact, `ssl` still imports, and both plain and
    TLS connections raise.
    """
    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked, raising=True)
    monkeypatch.setattr(socket.socket, "sendto", _blocked, raising=True)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    # Minting application default credentials can reach the metadata server or a browser flow.
    # Importing google.auth is fine; calling `default` is not.
    try:
        import google.auth

        monkeypatch.setattr(google.auth, "default", _blocked, raising=False)
    except ImportError:
        pass
    yield


@pytest.fixture(scope="session")
def fixtures():
    """The tests/fixtures directory, which holds JSON fixtures in subdirectories."""
    return REPO / "tests" / "fixtures"
