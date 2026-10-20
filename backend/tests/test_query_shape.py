"""Queries must not get slower as the book grows.

A one-process, one-SQLite-file program does not fail at scale by tipping over --
it fails by **shape**. A loop that asks the database one question per customer
is three queries at three customers and ten thousand round trips at ten
thousand, and the same request that was instant in development becomes the
thing everybody waits for. Measured on a synthetic book (see
``scripts/scale_probe.py``), the holiday-greeting path before this file's fix
cost 340 ms at 500 customers, 1.4 s at 2 000 and **9.2 s at 10 000** -- one
round trip per customer, exactly the shape the plan and churn paths had
already been batched away from.

So this file pins the shapes, not the timings. A timing assertion would be
flaky on a loaded machine; "the number of statements does not grow with the
number of rows" is a property, and it holds or it does not.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import event

from app.services.agent_service import generate_holiday_emails


async def count_statements(db, coro):
    """Run ``coro`` and return (result, list of SQL statements it issued)."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        result = await coro
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)
    return result, statements


async def a_customer_with_contact(db, index: int, stage: str = "completed"):
    from app.models.customer import Customer, CustomerContact

    # ``completed`` on purpose: the holiday query only excludes ``lost``, so
    # these customers are in its book -- but the *follow-up plan* covers
    # contacted/interested/quoting only, and the plan is a top-N list over the
    # whole book. A planned-stage customer left behind by this file can push
    # another test's customer out of that list (it has done so before; see the
    # note in test_approval_gate.py).
    customer = Customer(
        company_name=f"Query Shape {index}",
        country="DE",
        stage=stage,
    )
    db.add(customer)
    await db.flush()
    db.add(CustomerContact(
        customer_id=customer.id,
        name=f"Buyer {index}",
        email=f"buyer{index}@example.test",
        is_primary=1,
    ))
    await db.commit()
    return customer.id


async def test_the_holiday_greeting_loads_contacts_in_one_query_not_one_per_customer(db):
    """The book's size must not decide how many round trips a request makes.

    Every customer needs their primary contact. Asking per customer is the
    shape this codebase already criticises in writing ("one query per customer
    each for emails and contacts is a hundred round trips at a hundred
    customers"); the holiday path had not been given the same treatment. The
    count is the assertion: one ``customer_contacts`` read, however many
    customers there are.

    Assertions are about *this test's* rows only. The shared test database
    holds the demo book and whatever earlier tests left behind, so counting
    the whole book is how one test starts failing because another ran first.
    """
    ids = [await a_customer_with_contact(db, i) for i in range(8)]

    result, statements = await count_statements(
        db,
        generate_holiday_emails("Christmas", company_name="Acme", db_session=db),
    )

    contact_reads = [s for s in statements if "FROM customer_contacts" in s]
    customer_reads = [s for s in statements if "FROM customers" in s]
    assert len(contact_reads) == 1, (
        f"one query for every customer's primary contact, got {len(contact_reads)}: "
        f"{contact_reads}"
    )
    assert len(customer_reads) == 1, f"one query for the book: {customer_reads}"

    drafted = {e["customer_id"] for e in result["emails"]}
    for customer_id in ids:
        assert customer_id in drafted, (
            f"customer {customer_id} has a contact and must get a draft"
        )


async def test_the_holiday_greeting_still_skips_customers_without_a_contact(db):
    """Batching must not change who gets an email.

    A customer with no primary contact (or none with an address) is skipped,
    and one with a contact is included -- the per-customer lookup used to
    guarantee that by construction; the batch has to keep it by mapping.
    """
    with_contact = await a_customer_with_contact(db, 1)

    from app.models.customer import Customer

    db.add(Customer(company_name="No Contact Ltd", country="DE", stage="completed"))
    await db.commit()

    result, _ = await count_statements(
        db,
        generate_holiday_emails("Christmas", company_name="Acme", db_session=db),
    )

    drafted = {e["customer_id"]: e["to_email"] for e in result["emails"]}
    assert with_contact in drafted, "the customer with an address gets a draft"
    assert drafted[with_contact] == "buyer1@example.test"

    # The one without a contact: find its id and check it is absent.
    from sqlalchemy import select

    no_contact = (await db.execute(
        select(Customer).where(Customer.company_name == "No Contact Ltd")
    )).scalar_one()
    assert no_contact.id not in drafted, (
        "a customer with no address must not appear in the draft list"
    )


async def test_the_holiday_batch_keeps_each_contact_on_its_own_customer(db):
    """Keyed by customer id, never by position (the same rule as the plan).

    Two customers with different contacts must not end up with each other's
    addresses -- pairing two result lists by index is how one customer's mail
    ends up going to another.
    """
    ids = [await a_customer_with_contact(db, i) for i in (1, 2, 3)]

    result, _ = await count_statements(
        db,
        generate_holiday_emails("Christmas", company_name="Acme", db_session=db),
    )

    by_customer = {e["customer_id"]: e["to_email"] for e in result["emails"]}
    for i, customer_id in enumerate(ids):
        assert by_customer[customer_id] == f"buyer{i + 1}@example.test"


async def test_the_plan_does_not_read_email_bodies(db):
    """The plan needs timestamps and status, not the text of the emails.

    At 25 000 customers this query returns 600 000 rows. Selecting whole rows
    made the plan peak at 1.1 GB of Python heap in the same process that serves
    the API; it now streams one customer's history at a time and loads only the
    columns it reads. The column list is the part a test can pin:
    ``derive_thread_state`` uses status/sent_at/created_at/replied_at, and the
    failed-attempt branch uses error_message. ``body`` is the largest column in
    the table and nothing here reads it.
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.models.customer import Customer
    from app.models.email_log import EmailLog, EmailStatus
    from app.services.agent_service import _plan_inputs_for

    # ``completed`` keeps these rows out of the follow-up plan's book while
    # still being passed to ``_plan_inputs_for`` explicitly. A planned-stage
    # customer left behind here pushes another test's customer out of the
    # plan's top-N -- the ordering fragility this suite has hit three times.
    ids = [await a_customer_with_contact(db, i) for i in range(1, 6)]
    for customer_id in ids:
        db.add(EmailLog(
            customer_id=customer_id,
            to_email="buyer@example.test",
            to_name="Buyer",
            subject="quote",
            body="BODY-MARKER " + ("x" * 500),
            status=EmailStatus.FAILED,
            error_message="550 rejected",
        ))
    await db.commit()

    customers = (
        await db.execute(select(Customer).where(Customer.id.in_(ids)))
    ).scalars().all()
    inputs, statements = await count_statements(
        db, _plan_inputs_for(db, customers, datetime.utcnow())
    )

    email_reads = [s for s in statements if "FROM email_logs" in s]
    assert len(email_reads) == 1, email_reads
    assert "email_logs.body" not in email_reads[0]

    # ...and the rows that were not selected still produce the same decisions.
    assert all(i.has_any_email for i in inputs)
    assert {i.last_failed_reason for i in inputs} == {"550 rejected"}

