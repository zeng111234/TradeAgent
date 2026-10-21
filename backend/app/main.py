"""TradeAgent - FastAPI application entry point."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import access
from app.config import settings
from app.database import init_db
from app.errors import (
    ApiError,
    ErrorCode,
    accept_request_id,
    http_error_response,
    validation_error_response,
)
from app.errors import error_response as build_error_response
from app.routers import (
    agent,
    agent_runs,
    analytics,
    customers,
    diagnostics,
    emails,
    senders,
    tasks,
    usage,
)
from app.scheduler import start_scheduler, stop_scheduler, get_job_status, trigger_daily_routine
from app.timeouts import READY_TIMEOUT_SECONDS

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting TradeAgent...")
    # Initialize database
    await init_db()
    logger.info("Database initialized.")
    # Fail the runs a previous process abandoned. A row left as "running" can
    # never finish, and a poller cannot tell it from one that is still working,
    # so it waits forever for an answer that is not coming.
    await _fail_abandoned_runs()
    # Resolve email sends a previous process claimed but never finished. Whether
    # those mails arrived is not knowable, so they are failed with that said out
    # loud rather than re-queued and risked twice.
    await _resolve_abandoned_sends()
    # Insert demo data if empty
    await _insert_demo_data()
    # Start the scheduler
    start_scheduler()
    logger.info("TradeAgent is ready!")
    yield
    # Let a run in flight finish rather than killing it mid-write. Anything that
    # does not make it stays "running" and is failed on the next start.
    try:
        from app.routers.agent_runs import drain_background_runs

        await drain_background_runs(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not drain background runs: %s", exc)
    # Stop scheduler on shutdown
    stop_scheduler()
    logger.info("Shutting down TradeAgent...")


async def _fail_abandoned_runs() -> None:
    try:
        from app.agent.repository import mark_stale_runs
        from app.database import async_session

        async with async_session() as db:
            await mark_stale_runs(db)
    except Exception as exc:  # noqa: BLE001 - never block startup for housekeeping
        logger.warning("could not sweep abandoned agent runs: %s", exc)


async def _resolve_abandoned_sends() -> None:
    try:
        from app.database import async_session
        from app.services.email_claims import mark_stale_sends

        async with async_session() as db:
            await mark_stale_sends(db)
    except Exception as exc:  # noqa: BLE001 - never block startup for housekeeping
        logger.warning("could not resolve abandoned email claims: %s", exc)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered foreign trade customer acquisition and email marketing assistant",
    lifespan=lifespan,
)

# The access gate, added before the request-id middleware so the id stays
# outermost: a request this refuses still gets an id, which is the whole point
# of the id. Inert unless APP_PASSWORD is set -- see app/access.py.
app.add_middleware(access.AccessGate)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _strip_trailing_slash(request: Request, call_next):
    """Send "/api/v1/customers/" to "/api/v1/customers", as before.

    Starlette already did this, but only while nothing matched the longer
    path -- and once the SPA fallback exists it matches everything, so the
    redirect stopped happening and every caller using a trailing slash started
    getting 404 (or 405 on POST). Leaving it broken would have been a silent
    change to endpoints that never moved, so the redirect is stated here
    instead of being inherited from a match order that no longer holds.

    Method and query are preserved: a 307 keeps the body and the verb, which
    is what makes a retried POST safe rather than lost.
    """
    path = request.url.path
    if path != "/" and path.endswith("/"):
        target = path.rstrip("/") or "/"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=307)
    return await call_next(request)


@app.middleware("http")
async def _request_id(request: Request, call_next):
    """Give every request an id, and put it on the response.

    The id is in the body of every error and in the log line that records it,
    which is the whole point: "it failed at 14:02" is not greppable, a
    sixteen-character id is. A caller's own id is honoured when it is safe to
    put in a header and a log line, so a trace spans both sides.

    Added *after* CORS on purpose: Starlette runs the last-added middleware
    outermost, so this one still sees the response CORS has already marked.
    """
    request.state.request_id = accept_request_id(request.headers.get("X-Request-Id"))
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return http_error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def _invalid_body(request: Request, exc: RequestValidationError):
    return validation_error_response(request, exc)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """A 500 that is still JSON, still has an id, and still says nothing.

    Without this, an unhandled exception leaves the process as Starlette's
    plain-text "Internal Server Error" -- a body no client can parse, no code,
    no id, and nothing tying it to a log line. The exception text is logged
    with the id and *not* returned: a stack trace in a response tells an
    attacker the library versions and tells the caller nothing they can act on.
    """
    logger.exception(
        "unhandled error on %s %s request_id=%s",
        request.method,
        request.url.path,
        getattr(request.state, "request_id", None),
    )
    return build_error_response(
        request,
        500,
        "internal_error",
        "The server failed while handling this request. It has been logged; "
        "quote the request_id when reporting it.",
        retryable=False,
    )


@app.exception_handler(sqlalchemy.exc.TimeoutError)
async def _pool_exhausted(request: Request, exc: Exception):
    """A busy server, said out loud.

    SQLAlchemy raises ``TimeoutError`` when a request waited ``pool_timeout`` for
    a free connection and never got one. Left alone this surfaces as a 500 --
    "something is broken" -- when the truth is "we are at capacity, come back".
    The distinction matters: a 500 sends an operator looking for a bug, and a
    client that retries a 500 is retrying a request the server never even started.

    Measured before this was added: with 100 concurrent requests the event loop
    stayed responsive (``/health`` p50 0 ms, max 16 ms) while every request
    queued behind fifteen pooled connections. The pool is the constraint, and it
    should say so.
    """
    from app.database import (
        DEFAULT_MAX_OVERFLOW,
        DEFAULT_POOL_SIZE,
        DEFAULT_POOL_TIMEOUT_SECONDS,
    )

    logger.warning(
        "connection pool exhausted: %s %s waited %.0fs",
        request.method, request.url.path, DEFAULT_POOL_TIMEOUT_SECONDS,
    )
    return build_error_response(
        request,
        503,
        "db_pool_exhausted",
        (
            "The server is at capacity. No free database connection was "
            f"available within {DEFAULT_POOL_TIMEOUT_SECONDS:.0f}s "
            f"({DEFAULT_POOL_SIZE + DEFAULT_MAX_OVERFLOW} in the pool). "
            "This request was not started; retry shortly."
        ),
        retryable=True,
        headers={"Retry-After": "2"},
    )


# Register routers
app.include_router(customers.router, prefix="/api/v1")
app.include_router(emails.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
# Goal-driven runs and their traces. Separate router so the reviewed,
# human-triggered endpoints above stay untouched.
app.include_router(agent_runs.router, prefix="/api/v1")
# What the models have cost, by scope. Read-only view of the in-process ledger.
app.include_router(usage.router, prefix="/api/v1")
# Circuit states, run slots and pool occupancy. Read-only, changes nothing.
app.include_router(diagnostics.router, prefix="/api/v1")
# The mailboxes a send can go out from. Separate router because switching
# sender is an identity decision: the agent's tools never touch these rows.
app.include_router(senders.router, prefix="/api/v1")
# Sign-in for the shared-password gate. No prefix: it is the front door, not
# part of the API, and it must answer before anything else is reachable.
app.include_router(access.router)


def _app_info() -> dict:
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "message": "TradeAgent - AI Foreign Trade Customer Acquisition Assistant",
    }


@app.get("/api/v1/info")
async def info():
    """What this deployment is.

    Moved here from ``/`` because a single-link deployment serves the app at
    the root, and the greeting that used to live there is worth keeping
    somewhere a client can read it.
    """
    return _app_info()


@app.get("/")
async def root():
    """The app, when a build was shipped; the greeting when it was not.

    Both are honest answers to the same request. Before ``webroot`` existed
    this returned the greeting as JSON, and it still does on a checkout with
    no built frontend -- which is every test run and every API-only install.
    """
    index = _spa_index()
    if index is None:
        return _app_info()
    return FileResponse(index)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """Ready to serve, which is not the same as running.

    ``/health`` answers for the process; a process that is alive but cannot
    reach its database will keep answering 200 there while every real request
    fails. This asks the database one question with a two-second ceiling, and
    fails closed: a false "ready" sends traffic into a hole, a false "not ready"
    costs a retry.

    No session is held: the connection is opened, asked once, and returned. This
    route is polled every few seconds by whatever is in front of the service.
    """
    from sqlalchemy import text

    from app.database import async_session

    try:
        async with async_session() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=READY_TIMEOUT_SECONDS
            )
    except Exception as exc:  # noqa: BLE001 - every failure means "not ready"
        logger.warning("readiness probe failed: %s: %s", type(exc).__name__, exc)
        raise ApiError(
            status_code=503,
            code=ErrorCode.DATABASE_UNAVAILABLE,
            detail=(
                "the database did not answer a SELECT 1 within "
                f"{READY_TIMEOUT_SECONDS:.0f}s ({type(exc).__name__}). This "
                "instance should not receive traffic until it does."
            ),
            retryable=True,
        ) from exc
    return {"status": "ready", "database": "ok"}


@app.get("/api/v1/scheduler/jobs")
async def api_scheduler_jobs():
    """Get status of all scheduled jobs."""
    return {"jobs": get_job_status()}


@app.get("/api/v1/scheduler/history")
async def api_scheduler_history():
    """What the scheduler actually did -- including the runs it skipped.

    `/scheduler/jobs` answers "when will it run next". This answers "did it run",
    which is the question somebody asks after a report failed to arrive. A skipped
    fire is recorded here rather than dropped, because a skipped scheduled run is
    a missing report and used to be indistinguishable from one never scheduled.
    """
    from app.scheduler import get_job_history

    return await get_job_history()


@app.post("/api/v1/scheduler/run-daily")
async def api_run_daily(search_keywords: str = "textile buyer", target_country: str = ""):
    """Manually trigger the daily morning routine."""
    result = await trigger_daily_routine(search_keywords=search_keywords, target_country=target_country)
    return result


async def _insert_demo_data():
    """Insert demo data if the database is empty."""
    from sqlalchemy import select, func
    from app.database import async_session
    from app.models.customer import Customer, CustomerContact, CustomerNote
    from app.models.email_template import EmailTemplate
    from app.models.task import Task

    async with async_session() as db:
        # Check if data exists
        result = await db.execute(select(func.count(Customer.id)))
        count = result.scalar()
        if count > 0:
            return

        logger.info("Inserting demo data...")

        # Demo customers (textile industry)
        customers_data = [
            Customer(
                company_name="ABC Fashion Imports LLC",
                country="United States",
                city="New York",
                website="https://www.abcfashion.com",
                industry="Textile & Apparel",
                products="embroidery thread, metallic yarn, decorative fabric",
                source="scraper",
                stage="interested",
                score=85.0,
                tags="vip,textile,usa",
                annual_import_value="$500,000+",
            ),
            Customer(
                company_name="EuroTextil GmbH",
                country="Germany",
                city="Hamburg",
                website="https://www.eurotextil.de",
                industry="Textile & Embroidery",
                products="gold thread, embroidery supplies, metallic thread",
                source="import",
                stage="quoting",
                score=72.0,
                tags="europe,textile,embroidery",
                annual_import_value="$300,000+",
            ),
            Customer(
                company_name="Mediterranean Textile S.L.",
                country="Spain",
                city="Barcelona",
                website="https://www.medtextile.es",
                industry="Fashion Accessories",
                products="gold lace, metallic ribbon, decorative thread",
                source="exhibition",
                stage="new",
                score=45.0,
                tags="textile,spain,fashion",
            ),
            Customer(
                company_name="Japan Embroidery Co., Ltd",
                country="Japan",
                city="Tokyo",
                website="https://www.japanembroidery.co.jp",
                industry="Embroidery & Textile",
                products="embroidery thread, gold metallic yarn, sequins",
                source="alibaba",
                stage="contacted",
                score=68.0,
                tags="textile,japan,embroidery",
                annual_import_value="$1,200,000+",
            ),
            Customer(
                company_name="Ningbo Sunshine Textile Co., Ltd",
                company_name_cn="Ningbo Sunshine Textile Co., Ltd",
                country="China",
                city="Ningbo",
                industry="Textile Manufacturing",
                products="gold thread, metallic yarn, embroidery supplies",
                source="referral",
                stage="completed",
                score=95.0,
                tags="local,nb,partner,textile",
                annual_import_value="$2,000,000+",
            ),
        ]

        for c in customers_data:
            db.add(c)
        await db.flush()

        # Add contacts
        contacts_data = [
            CustomerContact(customer_id=customers_data[0].id, name="John Smith", title="Purchasing Manager", email="john@abcfashion.com", phone="+1-212-555-0100", is_primary=1),
            CustomerContact(customer_id=customers_data[1].id, name="Hans Mueller", title="Import Director", email="hans@eurotextil.de", phone="+49-40-555-0200", is_primary=1),
            CustomerContact(customer_id=customers_data[3].id, name="Yuki Tanaka", title="Sourcing Manager", email="yuki@japanembroidery.co.jp", phone="+81-3-5555-0400", is_primary=1),
        ]
        for c in contacts_data:
            db.add(c)

        # Add notes
        notes_data = [
            CustomerNote(customer_id=customers_data[0].id, content="Met at Canton Fair 2025, very interested in our gold metallic yarn", note_type="meeting"),
            CustomerNote(customer_id=customers_data[0].id, content="Sent textile catalog and embroidery thread samples via email", note_type="email"),
            CustomerNote(customer_id=customers_data[1].id, content="Requested samples for gold thread and metallic yarn, 3 variants", note_type="quotation"),
        ]
        for n in notes_data:
            db.add(n)

        # Demo templates
        templates = [
            EmailTemplate(
                name="Cold Outreach - General",
                subject="Quality {product_name} from {company_name}",
                body="<p>Dear {contact_name},</p><p>We are {company_name}, a leading manufacturer of {product_name} based in Ningbo, China.</p><p>We would love to discuss how we can support your business with high-quality products at competitive prices.</p><p>Best regards,<br/>{company_name}</p>",
                language="en",
                category="cold_outreach",
            ),
            EmailTemplate(
                name="Follow-up Template",
                subject="Following up - {product_name} inquiry",
                body="<p>Dear {contact_name},</p><p>I hope this message finds you well. I wanted to follow up on our previous correspondence regarding {product_name}.</p><p>Would you have time for a quick call this week?</p><p>Best regards,<br/>{company_name}</p>",
                language="en",
                category="follow_up",
            ),
        ]
        for t in templates:
            db.add(t)

        # Demo tasks
        tasks_data = [
            Task(title="Follow up with ABC Fashion on embroidery thread samples", customer_id=customers_data[0].id, priority="high", task_type="follow_up"),
            Task(title="Send updated price list for metallic yarn to EuroTextil", customer_id=customers_data[1].id, priority="medium", task_type="quotation"),
            Task(title="Prepare Canton Fair booth for textile exhibition", priority="low", task_type="meeting"),
        ]
        for t in tasks_data:
            db.add(t)

        await db.commit()
        logger.info("Demo data inserted successfully!")

# --------------------------------------------------------------------------
# The built frontend, when one was shipped
# --------------------------------------------------------------------------
# Everything below is conditional on the build being present. An API-only
# checkout -- every test run, every "just the backend" install -- behaves
# exactly as it did before this section existed, including what "/" answers.

def _webroot() -> Path:
    """Directory holding the built frontend.

    Relative to this file, not to the process: a published app is started from
    wherever the sandbox puts it, and a path that only works from one
    directory is a bug that shows up as a 404 on the one machine nobody
    tested. ``parent.parent`` because this file lives in ``app/`` and the
    build lands beside it, in the package root.
    """
    return Path(__file__).resolve().parent.parent / settings.WEBROOT


def _spa_index() -> Path | None:
    """The app's entry document, or None when no build was shipped."""
    index = _webroot() / "index.html"
    return index if index.is_file() else None


_ASSETS = _webroot() / "assets"
if _ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """Answer every path the API did not claim with the app itself.

    Registered last on purpose: Starlette matches routes in registration
    order, so the routers above keep every ``/api/v1/**`` path and this only
    sees what is left. That is what makes a client-side route work on a cold
    load or a refresh -- ``/customers/3`` has to reach the app, not a 404
    from a server that has never heard of it.

    An API-shaped path that nothing claimed is still a 404 with a body that
    says so: answering an unknown endpoint with HTML is how a caller ends up
    parsing a login page as JSON.
    """
    api_shaped = (
        full_path.startswith("api/")
        or full_path.startswith("openapi")
        or full_path in {"docs", "docs/oauth2-redirect", "redoc"}
    )
    index = _spa_index()
    if api_shaped or index is None:
        raise ApiError(
            status_code=404,
            code=ErrorCode.NOT_FOUND,
            detail=f"no such route: /{full_path}",
        )
    return FileResponse(index)
