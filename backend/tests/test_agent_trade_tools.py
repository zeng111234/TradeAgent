"""Integration tests for the CRM tools against a real (test) database.

Uses the demo seed created by ``main._insert_demo_data`` through the ``client``
fixture, so the data these tools see is the same shape the app produces.
"""
from __future__ import annotations

import uuid

import pytest

from app.agent.trade_tools import (
    classify_prospect,
    create_email_draft,
    get_communication_history,
    get_customer,
    get_customer_signals,
    list_customers,
    upsert_customer,
)

pytestmark = pytest.mark.usefixtures("client")


async def _any_customer_id(db) -> int:
    from sqlalchemy import select

    from app.models.customer import Customer

    row = (await db.execute(select(Customer).limit(1))).scalar_one()
    return row.id


# --- list_customers --------------------------------------------------------

async def test_list_customers_returns_the_seeded_pipeline(db):
    result = await list_customers()
    # The demo seed has five customers, one of them stage="completed", which
    # the default filter correctly hides.
    assert result["count"] >= 4
    assert len(result["customers"]) == result["count"]

    first = result["customers"][0]
    for key in ("id", "company_name", "stage", "score"):
        assert key in first
    # Sorted by score descending.
    scores = [c["score"] for c in result["customers"]]
    assert scores == sorted(scores, reverse=True)


async def test_list_customers_excludes_closed_stages_by_default(db):
    default = await list_customers()
    assert all(c["stage"] not in ("completed", "lost") for c in default["customers"])

    everything = await list_customers(include_closed=True)
    assert everything["count"] >= default["count"]
    assert any(c["stage"] in ("completed", "lost") for c in everything["customers"])


async def test_list_customers_honours_stage_filter(db):
    result = await list_customers(stage="quoting")
    assert all(c["stage"] == "quoting" for c in result["customers"])


async def test_list_customers_honours_country_filter(db):
    result = await list_customers(country="Germany", include_closed=True)
    assert all("germany" in (c["country"] or "").lower() for c in result["customers"])


async def test_list_customers_search_matches_company_or_products(db):
    result = await list_customers(search="embroidery", include_closed=True)
    assert result["count"] >= 1


async def test_list_customers_min_score_filter(db):
    result = await list_customers(min_score=60, include_closed=True)
    assert all(c["score"] >= 60 for c in result["customers"])


async def test_list_customers_limit_is_capped(db):
    """A model asking for 10_000 rows must not get them."""
    result = await list_customers(limit=10_000, include_closed=True)
    assert len(result["customers"]) <= 25


async def test_list_customers_empty_result_is_not_an_error(db):
    result = await list_customers(search="zzz-no-such-company-zzz")
    assert result["count"] == 0
    assert result["customers"] == []


# --- get_customer ----------------------------------------------------------

async def test_get_customer_returns_contacts_and_notes(db):
    customer_id = await _any_customer_id(db)
    result = await get_customer(customer_id=customer_id)
    assert result["id"] == customer_id
    assert "contacts" in result and isinstance(result["contacts"], list)
    assert "recent_notes" in result
    assert result["open_tasks"] is not None


async def test_get_customer_missing_id_is_an_error_observation(db):
    result = await get_customer(customer_id=999_999)
    assert "error" in result
    assert "999999" in result["error"]


# --- get_communication_history --------------------------------------------

async def test_communication_history_without_records(db):
    customer_id = await _any_customer_id(db)
    result = await get_communication_history(customer_id=customer_id)
    assert "messages" in result
    assert isinstance(result["messages"], list)
    if result["message_count"] == 0:
        assert "no email records" in result["note"]


# --- get_customer_signals --------------------------------------------------

async def test_signals_return_numbers_not_a_verdict(db):
    """The whole point: no risk label is manufactured here."""
    customer_id = await _any_customer_id(db)
    result = await get_customer_signals(customer_id=customer_id)

    assert "risk_level" not in result
    assert "risk_reasons" not in result
    assert "suggested_actions" not in result

    for key in ("days_since_last_contact", "days_in_current_stage", "emails_sent",
                "emails_replied", "never_contacted", "has_any_reply"):
        assert key in result


async def test_signals_missing_customer(db):
    assert "error" in await get_customer_signals(customer_id=999_999)


# --- classify_prospect -----------------------------------------------------

async def test_classify_prospect_returns_a_rubric_and_no_score():
    result = await classify_prospect(
        page_text="We import embroidery thread and metallic yarn for the German market.",
        target_products="gold metallic yarn",
    )

    # No score is produced by the tool — that is the model's job.
    assert "score" not in result
    assert "recommendation" not in result

    assert len(result["rubric"]) >= 3
    for dimension in result["rubric"]:
        assert {"dimension", "question", "look_for"} <= set(dimension)

    assert "score" in result["required_output"]
    assert "recommendation" in result["required_output"]
    assert "evidence" in result["required_output"]["reasoning"]
    assert "German market" in result["page_text"]


async def test_classify_prospect_truncates_large_pages():
    result = await classify_prospect(
        page_text="x" * 50_000, target_products="yarn"
    )
    assert len(result["page_text"]) <= 3000


# --- upsert_customer ------------------------------------------------------

async def test_upsert_creates_then_skips_a_duplicate(db):
    unique = f"Pytest Prospect {uuid.uuid4().hex[:8]}"

    created = await upsert_customer(
        company_name=unique,
        website=f"https://{uuid.uuid4().hex[:8]}.example.com",
        country="Germany",
    )
    assert created["status"] == "created"
    new_id = created["customer_id"]

    try:
        duplicate = await upsert_customer(company_name=unique, country="France")
        assert duplicate["status"] == "skipped"
        assert duplicate["existing_customer_id"] == new_id
    finally:
        await _delete_customer(db, new_id)


async def test_upsert_detects_an_existing_website(db):
    unique_site = f"https://{uuid.uuid4().hex[:8]}.example.com"
    name_a = f"Pytest Site A {uuid.uuid4().hex[:6]}"
    name_b = f"Pytest Site B {uuid.uuid4().hex[:6]}"

    created = await upsert_customer(company_name=name_a, website=unique_site)
    assert created["status"] == "created"

    try:
        second = await upsert_customer(company_name=name_b, website=unique_site)
        assert second["status"] == "skipped"
    finally:
        await _delete_customer(db, created["customer_id"])


async def test_upsert_dry_run_writes_nothing(db):
    from sqlalchemy import select

    from app.models.customer import Customer

    unique = f"Pytest DryRun {uuid.uuid4().hex[:8]}"
    result = await upsert_customer(company_name=unique, dry_run=True)

    assert result["status"] == "would_create"
    assert result["dry_run"] is True

    existing = (
        await db.execute(select(Customer).where(Customer.company_name == unique))
    ).scalar_one_or_none()
    assert existing is None


async def test_upsert_requires_a_company_name(db):
    assert "error" in await upsert_customer(company_name="   ")


async def test_upsert_never_modifies_an_existing_customer(db):
    """An agent must not silently rewrite curated data."""
    from sqlalchemy import select

    from app.models.customer import Customer

    unique = f"Pytest Immutable {uuid.uuid4().hex[:8]}"
    created = await upsert_customer(company_name=unique, country="Germany", score=10.0)
    new_id = created["customer_id"]

    try:
        await upsert_customer(company_name=unique, country="France", score=99.0)
        row = (
            await db.execute(select(Customer).where(Customer.id == new_id))
        ).scalar_one()
        assert row.country == "Germany", "existing customer was overwritten"
        assert row.score == 10.0, "existing score was overwritten"
    finally:
        await _delete_customer(db, new_id)


# --- create_email_draft ---------------------------------------------------

async def test_create_draft_records_a_pending_row_and_sends_nothing(db):
    from sqlalchemy import select

    from app.models.email_log import EmailLog, EmailStatus

    unique = f"Pytest Draft Co {uuid.uuid4().hex[:8]}"
    created = await upsert_customer(company_name=unique)
    customer_id = created["customer_id"]

    try:
        result = await create_email_draft(
            customer_id=customer_id,
            to_email="buyer@example.com",
            subject="Partnership enquiry",
            body="<p>Hello</p>",
        )
        assert result["status"] == "draft_created"
        assert "pending" in result["send_status"]

        row = (
            await db.execute(
                select(EmailLog).where(EmailLog.id == result["email_log_id"])
            )
        ).scalar_one()
        assert row.status == EmailStatus.PENDING
        assert row.sent_at is None
        assert row.tracking_id is None
    finally:
        await _purge_emails(db, customer_id)
        await _delete_customer(db, customer_id)


async def test_create_draft_refuses_to_invent_a_recipient(db):
    unique = f"Pytest NoEmail {uuid.uuid4().hex[:8]}"
    created = await upsert_customer(company_name=unique)
    customer_id = created["customer_id"]

    try:
        result = await create_email_draft(
            customer_id=customer_id, subject="Hi", body="<p>Hi</p>"
        )
        assert "error" in result
        assert "no email address" in result["error"]
    finally:
        await _delete_customer(db, customer_id)


async def test_create_draft_validates_subject_and_body(db):
    assert "error" in await create_email_draft(customer_id=1, subject="  ", body="x")
    assert "error" in await create_email_draft(customer_id=1, subject="x", body="  ")


async def test_create_draft_missing_customer(db):
    result = await create_email_draft(
        customer_id=999_999, subject="Hi", body="<p>Hi</p>", to_email="a@b.com"
    )
    assert "error" in result


# --- cleanup helpers ------------------------------------------------------

async def _delete_customer(db, customer_id: int) -> None:
    from sqlalchemy import delete

    from app.models.customer import Customer

    await db.execute(delete(Customer).where(Customer.id == customer_id))
    await db.commit()


async def _purge_emails(db, customer_id: int) -> None:
    from sqlalchemy import delete

    from app.models.email_log import EmailLog

    await db.execute(delete(EmailLog).where(EmailLog.customer_id == customer_id))
    await db.commit()


# --------------------------------------------------------------------------
# When the search provider changes its response shape
# --------------------------------------------------------------------------
# The tool reads three keys from each result: href, title, body. A provider
# that renames them returns a non-empty list that produces zero candidates and
# zero errors -- an empty market and a renamed field look identical from the
# outside. Only the raw count tells them apart, so the tool has to report it.

async def test_a_renamed_href_field_is_reported_as_a_shape_change(monkeypatch):
    import ddgs

    from app.agent.trade_tools import search_prospects

    class RenamedField:
        def text(self, query, max_results=8):
            # Results arrived; the field this tool reads is gone.
            return [
                {"url": "https://example-textil.example", "title": "Example", "snippet": "x"},
                {"url": "https://other.example", "title": "Other", "snippet": "y"},
            ]

    monkeypatch.setattr(ddgs, "DDGS", RenamedField)

    result = await search_prospects(keyword="embroidery thread", country="Germany")

    assert result["count"] == 0
    shape_errors = [e for e in result["errors"] if "shape may have changed" in e]
    assert shape_errors, (
        "a non-empty response with no usable href is a contract change, not an "
        f"empty market; errors were {result['errors']!r}"
    )
    assert "2 result(s)" in shape_errors[0], "the raw count is the evidence"


async def test_results_that_are_not_dicts_do_not_crash_the_tool(monkeypatch):
    """A provider that returns strings must be reported, not AttributeError."""
    import ddgs

    from app.agent.trade_tools import search_prospects

    class Strings:
        def text(self, query, max_results=8):
            return ["https://example-textil.example"]

    monkeypatch.setattr(ddgs, "DDGS", Strings)

    result = await search_prospects(keyword="embroidery thread")

    assert result["count"] == 0
    assert any("shape may have changed" in e for e in result["errors"])


async def test_a_genuinely_empty_search_is_not_reported_as_a_shape_change(monkeypatch):
    """An empty result list is an empty market -- do not cry drift at it."""
    import ddgs

    from app.agent.trade_tools import search_prospects

    class Empty:
        def text(self, query, max_results=8):
            return []

    monkeypatch.setattr(ddgs, "DDGS", Empty)

    result = await search_prospects(keyword="embroidery thread")

    assert result["count"] == 0
    assert result["errors"] == []
