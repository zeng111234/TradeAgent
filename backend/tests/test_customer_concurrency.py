"""Two people, one customer: what the writes do to each other.

The send path had its answer already (``app/services/email_claims.py``: a claim
is a lock). The customer record had none: whoever saved second overwrote
whichever fields the other person had changed, from a form loaded before them,
silently. And ``DELETE /customers/{id}`` cascaded to the email rows -- deleting
the *sent* records that the email log's own delete refuses to touch (R39) and
that a "the mail never arrived" investigation reads (R36). These tests pin the
two guards that close those holes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.email_log import EmailLog, EmailStatus


CUSTOMERS_URL = "/api/v1/customers"


async def make_customer(db, *, stage="completed"):
    """A customer at a stage the follow-up plan does not cover.

    The plan is a top-N list over the whole book (see R39's note on suite
    ordering): a planned-stage customer left behind here can push another
    test's customer off the list. These tests do not care about stage.
    """
    from app.models.customer import Customer

    suffix = uuid.uuid4().hex[:8]
    customer = Customer(
        company_name=f"TwoHands {suffix}",
        country="Germany",
        industry="textiles",
        stage=stage,
        source="manual",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


async def add_email(db, customer_id, *, status, subject="Hello"):
    row = EmailLog(
        customer_id=customer_id,
        to_email="buyer@example.test",
        subject=subject,
        body="<p>x</p>",
        status=status,
        tracking_id=f"t-{uuid.uuid4().hex[:10]}",
        created_at=datetime.utcnow() - timedelta(days=1),
        sent_at=datetime.utcnow() - timedelta(days=1)
        if status != EmailStatus.PENDING
        else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def reloaded(customer_id):
    """Read through a *fresh* session: the shared test session keeps copies."""
    from app.database import async_session
    from app.models.customer import Customer

    async with async_session() as fresh:
        return (
            await fresh.execute(select(Customer).where(Customer.id == customer_id))
        ).scalar_one_or_none()


async def log_count(customer_id) -> int:
    from app.database import async_session

    async with async_session() as fresh:
        rows = (
            await fresh.execute(
                select(EmailLog.id).where(EmailLog.customer_id == customer_id)
            )
        ).all()
        return len(rows)


async def drop(customer_id) -> None:
    """Delete this test's customer and whatever rows point at it."""
    from app.database import async_session
    from app.models.customer import Customer

    async with async_session() as fresh:
        for log in (
            await fresh.execute(
                select(EmailLog).where(EmailLog.customer_id == customer_id)
            )
        ).scalars().all():
            await fresh.delete(log)
        customer = (
            await fresh.execute(select(Customer).where(Customer.id == customer_id))
        ).scalar_one_or_none()
        if customer is not None:
            await fresh.delete(customer)
        await fresh.commit()


# --------------------------------------------------------------------------
# Deleting a customer must not smuggle out the evidence the log keeps
# --------------------------------------------------------------------------


async def test_a_customer_with_sent_mail_cannot_be_deleted(db, client):
    """The cascade would destroy exactly the rows the log refuses to delete.

    ``DELETE /logs/{id}`` guards a sent row (R39) because it is the record of
    what the customer actually received. Deleting the *customer* reached the
    same rows through ``cascade="all, delete-orphan"`` -- the same destruction,
    one level up. Now the customer stays, and the message says why.
    """
    customer = await make_customer(db)
    try:
        await add_email(db, customer.id, status=EmailStatus.SENT)

        r = client.delete(f"{CUSTOMERS_URL}/{customer.id}")

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "nothing was deleted" in detail
        assert "cannot be deleted" in detail

        assert await reloaded(customer.id) is not None, "the customer is still there"
        assert await log_count(customer.id) == 1, "and so is the record of the send"
    finally:
        await drop(customer.id)


async def test_a_customer_with_a_rejected_decision_cannot_be_deleted_either(db, client):
    """A rejection is a decision the next proposal must not repeat (R39)."""
    customer = await make_customer(db)
    try:
        await add_email(db, customer.id, status=EmailStatus.REJECTED)

        r = client.delete(f"{CUSTOMERS_URL}/{customer.id}")

        assert r.status_code == 409
        assert await reloaded(customer.id) is not None
    finally:
        await drop(customer.id)


async def test_a_customer_whose_mail_is_all_drafts_still_goes(db, client):
    """The guard must not protect more than it means to.

    Drafts are proposals; proposals travel with the customer. Only sent,
    attempted or decided rows are kept.
    """
    customer = await make_customer(db)
    try:
        await add_email(db, customer.id, status=EmailStatus.PENDING)

        r = client.delete(f"{CUSTOMERS_URL}/{customer.id}")

        assert r.status_code == 204
        assert await reloaded(customer.id) is None
        assert await log_count(customer.id) == 0, "the draft travels with the customer"
    finally:
        await drop(customer.id)


# --------------------------------------------------------------------------
# Two people editing the same customer
# --------------------------------------------------------------------------


async def test_a_stale_save_is_refused_rather_than_silently_winning(db, client):
    """Last-write-wins is how two people erase each other's work.

    The second saver's form was loaded *before* the first person's change, so
    every field in it is stale -- including the ones the second person never
    touched. With ``expected_updated_at`` the save is refused and nothing is
    changed, and the person is told to reload instead of quietly undoing a
    colleague.
    """
    customer = await make_customer(db)
    try:
        loaded_at = customer.updated_at

        # Person A saves first, moving the row.
        r = client.put(
            f"{CUSTOMERS_URL}/{customer.id}",
            json={"notes": "A's change", "expected_updated_at": loaded_at.isoformat()},
        )
        assert r.status_code == 200

        # Person B's form is from before A's save: stale.
        r = client.put(
            f"{CUSTOMERS_URL}/{customer.id}",
            json={"country": "France", "expected_updated_at": loaded_at.isoformat()},
        )

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "nothing was changed" in detail
        assert "Reload" in detail

        row = await reloaded(customer.id)
        assert row.notes == "A's change", "A's edit survives B's stale save"
        assert row.country == "Germany", "and B's stale fields did not land"
    finally:
        await drop(customer.id)


async def test_a_save_with_the_current_timestamp_still_goes_through(db, client):
    """The guard must not block the honest case it was built to protect."""
    customer = await make_customer(db)
    try:
        r = client.put(
            f"{CUSTOMERS_URL}/{customer.id}",
            json={
                "country": "France",
                "expected_updated_at": customer.updated_at.isoformat(),
            },
        )

        assert r.status_code == 200
        row = await reloaded(customer.id)
        assert row.country == "France"
    finally:
        await drop(customer.id)


async def test_without_the_field_the_write_still_lands(db, client):
    """Backward compatibility, stated rather than assumed.

    The API has callers that never heard of the guard (scripts, the import
    flow). Omitting the field keeps the old last-write-wins behaviour -- the
    guard is opt-in by sending it, not a new failure for old clients.
    """
    customer = await make_customer(db)
    try:
        r = client.put(f"{CUSTOMERS_URL}/{customer.id}", json={"country": "Spain"})

        assert r.status_code == 200
        row = await reloaded(customer.id)
        assert row.country == "Spain"
    finally:
        await drop(customer.id)


async def test_the_concurrency_field_never_reaches_the_row(db, client):
    """``expected_updated_at`` is a guard, not a column.

    It is dropped before the field copy; if it ever landed on the model it
    would be an unmapped attribute at best and a stray column at worst.
    """
    customer = await make_customer(db)
    try:
        r = client.put(
            f"{CUSTOMERS_URL}/{customer.id}",
            json={
                "tags": "checked",
                "expected_updated_at": customer.updated_at.isoformat(),
            },
        )
        assert r.status_code == 200

        row = await reloaded(customer.id)
        assert row.tags == "checked", "the mapped field still lands"
        assert "expected_updated_at" not in row.__dict__, (
            "the guard is not state; nothing about it is persisted"
        )
    finally:
        await drop(customer.id)
