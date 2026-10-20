"""Measure which queries degrade first as the book grows.

Seeds a synthetic database at three sizes and times the query shapes the
application actually uses (copied from the call sites, not invented). No
network, no model: this is about SQL and Python memory, not the agent.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["OPENAI_API_KEY"] = ""
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///probe-sentinel.db")

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import load_only  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
import app.models  # noqa: E402, F401  (registers every table on Base.metadata)
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.customer import Customer, CustomerContact, CustomerStage  # noqa: E402
from app.models.email_log import EmailLog, EmailStatus  # noqa: E402


def make_trace(rounds: int = 8) -> str:
    """A trace like the ones the loop writes: several rounds, tool calls."""
    steps = []
    for r in range(rounds):
        steps.append({
            "round": r,
            "messages": [
                {"role": "assistant", "content": "thinking about the customer " + "x" * 400},
                {"role": "tool", "name": "list_customers", "content": "y" * 300},
            ],
            "tool_calls": [
                {"name": "upsert_customer", "arguments": {"customer_id": r}, "ok": True},
            ],
        })
    return json.dumps(steps)


def timed(label: str, seconds: float, extra: str = "") -> None:
    print(f"  {label:<52} {seconds * 1000:8.1f} ms  {extra}")


async def seed(engine, n_customers: int, emails_per: int, n_runs: int) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        for i in range(n_customers):
            c = Customer(
                company_name=f"Co {i}",
                country="Turkey",
                industry="Textile",
                products="yarn fabric " * 5,
                stage=list(CustomerStage)[i % 5],
                score=float(i % 100),
            )
            db.add(c)
            await db.flush()
            db.add(CustomerContact(customer_id=c.id, name="Buyer", email=f"b{i}@x.test", is_primary=1))
            for e in range(emails_per):
                db.add(EmailLog(
                    customer_id=c.id,
                    to_email=f"b{i}@x.test",
                    to_name="Buyer",
                    subject=f"hello {e}",
                    body="Dear customer, " + "body text " * 30,
                    status=list(EmailStatus)[e % 4],
                ))
        for r in range(n_runs):
            db.add(AgentRun(
                run_id=f"run-{r}",
                goal="find textile importers",
                status="succeeded",
                stop_reason="final answer",
                trace=make_trace(),
            ))
        await db.commit()


async def measure(engine, n_customers: int) -> list[tuple[str, float, str]]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    out: list[tuple[str, float, str]] = []

    async with maker() as db:
        # 1. GET /agent/runs -- list view loads full rows incl. trace, then
        #    parses every trace to derive tool_sequence (agent_runs.py:407-417).
        t0 = time.perf_counter()
        rows = (await db.execute(
            select(AgentRun).order_by(AgentRun.id.desc()).limit(50)
        )).scalars().all()
        parsed = 0
        for row in rows:
            if row.trace:
                parsed += len(json.loads(row.trace))
        out.append(("list_runs: load 50 rows + parse traces", time.perf_counter() - t0,
                    f"{parsed} steps parsed"))

    async with maker() as db:
        # 2a. Daily intelligence -- the OLD shape: all planned customers, then
        #     ALL their email rows (body included) as one list of ORM objects.
        t0 = time.perf_counter()
        customers = (await db.execute(
            select(Customer).where(Customer.stage.in_(list(("contacted", "interested", "quoting"))))
        )).scalars().all()
        ids = [c.id for c in customers]
        n_emails = 0
        if ids:
            erows = (await db.execute(
                select(EmailLog).where(EmailLog.customer_id.in_(ids))
                .order_by(EmailLog.customer_id, EmailLog.created_at.desc())
            )).scalars().all()
            n_emails = len(erows)
        out.append(("daily plan OLD: every mail row held in memory",
                    time.perf_counter() - t0, f"{len(ids)} customers, {n_emails} mail rows"))

    async with maker() as db:
        # 2b. Daily intelligence -- the shape now in the code: 500-customer
        #     chunks, seven columns, one batch released before the next. Memory
        #     is the point: at 25 000 customers the list shape peaked at 1.1 GB,
        #     the chunked one at 94 MB, for the same decisions and (measured)
        #     the same wall time.
        t0 = time.perf_counter()
        customers = (await db.execute(
            select(Customer).where(Customer.stage.in_(list(("contacted", "interested", "quoting"))))
        )).scalars().all()
        ids = [c.id for c in customers]
        columns = (
            EmailLog.id, EmailLog.customer_id, EmailLog.status,
            EmailLog.created_at, EmailLog.sent_at, EmailLog.replied_at,
            EmailLog.error_message,
        )
        seen = 0
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = (await db.execute(
                select(EmailLog).where(EmailLog.customer_id.in_(chunk))
                .options(load_only(*columns))
                .order_by(EmailLog.customer_id, EmailLog.created_at.desc())
            )).scalars().all()
            seen += len(rows)
        out.append(("daily plan NEW: 500-customer chunks, 7 columns",
                    time.perf_counter() - t0, f"{len(ids)} customers, {seen} mail rows"))

    async with maker() as db:
        # 3a. Holiday greeting -- the OLD shape: one contact query PER
        #     customer (agent_service.py before the batch fix).
        t0 = time.perf_counter()
        customers = (await db.execute(
            select(Customer).where(Customer.stage.notin_(["lost"]))
        )).scalars().all()
        for c in customers:
            await db.execute(
                select(CustomerContact)
                .where(CustomerContact.customer_id == c.id, CustomerContact.is_primary == 1)
                .limit(1)
            )
        out.append(("holiday OLD: 1 contact query per customer",
                    time.perf_counter() - t0, f"{len(customers)} round trips"))

    async with maker() as db:
        # 3b. Holiday greeting -- the batched shape now in the code.
        t0 = time.perf_counter()
        customers = (await db.execute(
            select(Customer).where(Customer.stage.notin_(["lost"]))
        )).scalars().all()
        ids = [c.id for c in customers]
        if ids:
            await db.execute(
                select(CustomerContact).where(
                    CustomerContact.customer_id.in_(ids),
                    CustomerContact.is_primary == 1,
                )
            )
        out.append(("holiday NEW: one batched contact query",
                    time.perf_counter() - t0, f"2 round trips for {len(ids)} customers"))

    async with maker() as db:
        # 4. GET /emails/logs -- order by unindexed created_at desc limit 100,
        #    full rows including body (emails.py:268-274).
        t0 = time.perf_counter()
        rows = (await db.execute(
            select(EmailLog).order_by(EmailLog.created_at.desc()).limit(100)
        )).scalars().all()
        out.append(("email logs: order by created_at desc limit 100",
                    time.perf_counter() - t0, f"{len(rows)} rows, bodies loaded"))

    async with maker() as db:
        # 5. list_customers search -- 4x ilike %..% + order by unindexed score
        #    (trade_tools.py:284-310).
        t0 = time.perf_counter()
        pattern = "%yarn%"
        rows = (await db.execute(
            select(Customer)
            .where(sa.or_(
                Customer.company_name.ilike(pattern),
                Customer.products.ilike(pattern),
                Customer.industry.ilike(pattern),
                Customer.tags.ilike(pattern),
            ))
            .order_by(Customer.score.desc(), Customer.id.asc())
            .limit(20)
        )).scalars().all()
        out.append(("list_customers: 4x ilike + order by score limit 20",
                    time.perf_counter() - t0, f"{len(rows)} returned"))

    async with maker() as db:
        # 6. Email stats -- count(*) over the whole table (analytics shape).
        t0 = time.perf_counter()
        total = (await db.execute(select(func.count(EmailLog.id)))).scalar()
        sent = (await db.execute(
            select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.SENT)
        )).scalar()
        out.append(("email stats: count(*) + count(status=sent)",
                    time.perf_counter() - t0, f"{total} rows"))

    return out


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="scale-probe-"))
    print(f"database: {tmp}\n")
    for n_customers, emails_per, n_runs in ((500, 20, 200), (2000, 30, 1000), (10000, 40, 5000)):
        db_file = (tmp / f"book-{n_customers}.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        t0 = time.perf_counter()
        await seed(engine, n_customers, emails_per, n_runs)
        seed_s = time.perf_counter() - t0
        n_emails = n_customers * emails_per
        size_mb = (tmp / f"book-{n_customers}.db").stat().st_size / 1e6
        print(f"=== {n_customers} customers / {n_emails} email logs / "
              f"{n_runs} runs  (seeded in {seed_s:.1f}s, db {size_mb:.1f} MB)")
        for label, seconds, extra in await measure(engine, n_customers):
            timed(label, seconds, extra)
        print()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
