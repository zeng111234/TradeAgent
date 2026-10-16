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
    """
    with TestClient(app) as c:
        yield c


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
