"""The suite must fail closed when it tries to leave the machine.

The fixture lives in ``conftest``; this file proves it is *installed*, not merely
defined. A guard that stops applying (fixture renamed, marker skipped, an env
var left set) would leave the suite green while it quietly depends on whatever
the machine can reach -- which is exactly the failure this repository cannot see,
because the developer's laptop has a key and CI does not.

The second test pins the exception the guard needs: asyncio builds its self-pipe
with ``socketpair()``, so a guard that blocks *every* connect breaks the event
loop itself. That mistake was made once while writing this fixture.
"""
from __future__ import annotations

import os
import socket

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TRADEAGENT_ALLOW_NETWORK") == "1",
    reason="a deliberate live run has network enabled",
)


def test_a_connection_attempt_fails_with_the_seam_to_use_instead(refused_connections):
    with pytest.raises(RuntimeError, match="outbound network is blocked in tests"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)

    # Refused *and recorded*: a caller that catches Exception would otherwise
    # take its degraded path and call the test green.
    assert refused_connections


def test_the_message_says_what_to_do_instead(refused_connections):
    with socket.socket() as sock, pytest.raises(RuntimeError) as caught:
        sock.connect(("127.0.0.1", 9))

    assert "tests/fakes.py" in str(caught.value)


def test_the_event_loops_self_pipe_is_still_allowed():
    """Blocking every connect breaks anyio's portal, i.e. every API test."""
    left, right = socket.socketpair()
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
