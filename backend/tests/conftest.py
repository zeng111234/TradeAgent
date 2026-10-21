"""Shared pytest fixtures for TradeAgent.

Design notes
------------
Two seams make the whole app testable without network access:

1. **LLM seam** — every AI entrypoint in ``ai_service`` / ``agent_service``
   branches on ``settings.OPENAI_API_KEY``::

       if settings.OPENAI_API_KEY:
           return await _ai_analyze_website(...)   # needs network
       return _basic_analyze_website(...)          # deterministic

   We force that key empty so characterisation tests exercise the
   deterministic path. Tests that *do* want to exercise the LLM path
   install ``fake_llm`` (see ``tests/fakes.py``).

2. **DB seam** — ``settings.DATABASE_URL`` is read at import time, so we
   point it at a throwaway file *before* importing ``app.*``. The FastAPI
   dependency ``get_db`` is then overridden to the same engine so request
   handlers and direct service calls share one database.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# --- Make ``app`` importable (tests live in backend/tests, package in backend/app)
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# --- Environment must be configured before ``app.config`` is first imported.
_TMP_DB = Path(tempfile.mkdtemp(prefix="tradeagent-tests-")) / "test.db"
# aiosqlite wants forward slashes on Windows.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + _TMP_DB.as_posix())
os.environ["OPENAI_API_KEY"] = ""       # force deterministic fallbacks
os.environ["DEBUG"] = "false"           # silence SQLAlchemy echo
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
# A developer who is running the published build locally has APP_PASSWORD set
# in .env, and pydantic reads .env -- without this every test would be met by
# the gate's 401 instead of the endpoint it is testing. Set before import for
# the same reason the rest of this block is: environment beats .env.
os.environ["APP_PASSWORD"] = ""
os.environ.pop("REPORT_EMAIL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, async_session, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402

import app.config as app_config  # noqa: E402

# Keep the test database path stable even if an outer .env sets DATABASE_URL.
app_config.settings.DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create every table once for the session, and seed the demo data.

    The ``client`` fixture gets this for free from the app lifespan, but tests
    that only use ``db`` (repository, scan runner) would otherwise hit
    "no such table: agent_runs". Creating it here is idempotent.

    The demo seed is here too, and that part is not housekeeping. ``main``'s
    lifespan seeds only when the customer table is **empty**, so whichever test
    file creates the first customer decides whether the demo customers exist at
    all for the rest of the session -- and a file that only touches ``db`` never
    runs the lifespan. Adding one such file was enough to make four tests in a
    later file fail with "customer 4 not found", depending on the order pytest
    happened to collect them in. Seeding where the schema is created removes the
    ordering dependency instead of documenting it.
    """
    import asyncio

    from app.database import init_db
    from app.main import _insert_demo_data

    async def prepare():
        await init_db()
        await _insert_demo_data()

    asyncio.run(prepare())
    yield


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Every test starts with a closed breaker.

    The breaker is process-global state, and five transient failures in one test
    would otherwise refuse calls in the next one. Tests that exercise the breaker
    drive it explicitly; everything else deserves a clean process.
    """
    from app import circuit

    circuit.reset()
    yield
    circuit.reset()


@pytest.fixture
async def db():
    """A fresh async session bound to the test database."""
    async with async_session() as session:
        yield session


@pytest.fixture
def client():
    """FastAPI test client. Enters the lifespan so schema + demo data exist.

    The scheduler is started by the lifespan too; it only registers jobs and
    is torn down on exit, so it does not interfere.

    Background agent runs are drained before the client goes away. A run left
    in flight keeps writing rows after its test has finished, which is how a
    test that asserts "the list is empty" starts failing depending on the order
    pytest happened to pick.
    """
    with TestClient(app) as c:
        yield c
        try:
            from app.routers.agent_runs import drain_background_runs

            if _background_tasks_pending():
                asyncio.run(drain_background_runs(timeout=10.0))
        except Exception:  # noqa: BLE001 - teardown must not fail a test
            pass


def _background_tasks_pending() -> bool:
    from app.routers.agent_runs import _background_tasks

    return any(not task.done() for task in _background_tasks)


@pytest.fixture
async def seeded_db():
    """Dependency override so handler code uses the same engine as ``db``."""
    async def _override():
        async with async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """Belt-and-braces: no test may accidentally call a real LLM."""
    monkeypatch.setattr(app_config.settings, "OPENAI_API_KEY", "", raising=False)
    monkeypatch.setattr(app_config.settings, "SMTP_USER", "", raising=False)
    monkeypatch.setattr(app_config.settings, "SMTP_PASSWORD", "", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _no_outbound_network():
    """Make "the suite passes" mean "it passed without a network".

    Clearing ``OPENAI_API_KEY`` only closes the doors we remembered to close. A
    path that is not stubbed -- a new tool, a refactor that moves a call out
    from behind ``is_configured`` -- would then quietly use the real service on
    a developer laptop with credentials and take a different branch in CI
    without them. Both runs are green; neither describes the same code path.

    Connecting is therefore blocked outright, loopback included: ``TestClient``
    speaks ASGI in-process, so nothing legitimate in this suite opens a socket.
    The failure is a ``RuntimeError`` with the seam to use instead, because a
    test that fails here should be fixed at the seam, not marked slow.

    Refusals are also *recorded*, and the session fails at the end if any
    happened. Blocking alone is not enough: a caller that catches ``Exception``
    (the website fetch does, deliberately) would swallow the refusal and its
    test would go green down the degraded path -- proving the fallback, not that
    the test was offline.

    ``TRADEAGENT_ALLOW_NETWORK=1`` is the deliberate escape hatch for a live
    check; it is not meant to make a red test green.
    """
    if os.environ.get("TRADEAGENT_ALLOW_NETWORK") == "1":
        yield
        return

    import socket
    import traceback

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_socketpair = socket.socketpair

    def _caller() -> str:
        """The nearest frame in this repo, so the failure names a file, not a kernel."""
        for frame in reversed(traceback.extract_stack()[:-1]):
            name = frame.filename.replace("\\", "/")
            if name.endswith("/tests/conftest.py"):
                continue  # the guard's own frames say nothing about the caller
            if "/app/" in name or "/tests/" in name:
                return f"{name.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"
        return "unknown caller"

    # asyncio builds its self-pipe (and the test client's portal builds an
    # event loop) with ``socketpair()``, which on Windows is a loopback connect.
    # That one call has to pass; everything the *application* dials does not.
    # A counter rather than a host allow-list, because the sandbox proxy also
    # listens on 127.0.0.1 -- an allow-list would let real HTTP through here.
    allowed = 0

    def _blocked(self, address, *args, **kwargs):
        if allowed:
            return real_connect(self, address, *args, **kwargs)
        REFUSED_CONNECTIONS.append(f"{address!r} from {_caller()}")
        raise RuntimeError(
            f"outbound network is blocked in tests (connect to {address!r}). "
            "Replace the dependency at its seam (tests/fakes.py, ScriptedLLM, "
            "monkeypatch on the client module) instead of letting the test "
            "talk to a real service. See tests/conftest.py."
        )

    def _socketpair(*args, **kwargs):
        nonlocal allowed
        allowed += 1
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            allowed -= 1

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.socketpair = _socketpair
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.socketpair = real_socketpair

    if REFUSED_CONNECTIONS:
        attempts = ", ".join(REFUSED_CONNECTIONS[:5])
        pytest.fail(
            f"{len(REFUSED_CONNECTIONS)} outbound connection(s) were attempted "
            f"during the suite: {attempts}. A test that swallows the refusal is "
            "still a test that needs the network; stub it at the seam. Set "
            "TRADEAGENT_ALLOW_NETWORK=1 only for a deliberate live run.",
            pytrace=False,
        )


#: Every refused connect, in order. Session-scoped: the attempt may be made by a
#: thread or a background run after the test that caused it has moved on.
REFUSED_CONNECTIONS: list[str] = []


@pytest.fixture
def refused_connections():
    """Refusals made by the guard's own test, discarded so the session passes.

    The live list, not a slice: the attempt is appended *after* setup, so a
    copy taken at setup would always be empty (which is how this fixture first
    failed).
    """
    start = len(REFUSED_CONNECTIONS)
    yield REFUSED_CONNECTIONS
    del REFUSED_CONNECTIONS[start:]
