"""Integration tests for the CRM tools against a real (test) database.

Uses the demo seed created by ``main._insert_demo_data`` through the ``client``
fixture, so the data these tools see is the same shape the app produces.
"""
from __future__ import annotations

import uuid

import pytest

from app.agent.trade_tools import (
    add_customer_contact,
    add_customer_note,
    classify_prospect,
    complete_task,
    create_email_draft,
    create_email_template,
    create_task,
    get_communication_history,
    get_pipeline_stats,
    get_quality_report,
    list_email_logs,
    list_email_templates,
    list_tasks,
    get_customer,
    get_customer_signals,
    list_customers,
    reject_email_draft,
    render_email_template,
    reschedule_task,
    update_customer_stage,
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


# --- create_email_template ------------------------------------------------

async def test_create_template_writes_a_row_the_templates_page_lists(db):
    """The whole point: it lands where a person looks for templates."""
    from sqlalchemy import select

    from app.models.email_template import EmailTemplate

    unique = f"Pytest Template {uuid.uuid4().hex[:8]}"
    result = await create_email_template(
        name=unique,
        subject="Quality {product_name} for {company_name}",
        body="<p>Dear {contact_name},</p><p>Best regards</p>",
        category="cold_outreach",
        language="en",
    )

    assert result["status"] == "created"
    assert result["is_ai_generated"] is True
    try:
        row = (
            await db.execute(select(EmailTemplate).where(EmailTemplate.id == result["template_id"]))
        ).scalar_one()
        assert row.name == unique
        assert row.category == "cold_outreach"
        assert row.language == "en"
        assert row.is_ai_generated == 1
        assert row.use_count == 0
        # Same query the Templates page lists from -- if this returns the row,
        # the page shows the card.
        listed = (
            await db.execute(select(EmailTemplate).order_by(EmailTemplate.id))
        ).scalars().all()
        assert row.id in [t.id for t in listed]
    finally:
        await _purge_templates(db, result["template_id"])


async def test_create_template_reports_placeholders_and_flags_a_typo(db):
    unique = f"Pytest Vars {uuid.uuid4().hex[:8]}"
    result = await create_email_template(
        name=unique,
        subject="Hello {company_name}",
        body="<p>Hi {contct_name}, we make {product_name}.</p>",
        category="follow_up",
    )
    assert result["status"] == "created"
    assert set(result["placeholders_used"]) == {"company_name", "contct_name", "product_name"}
    assert result["unknown_placeholders"] == ["contct_name"]
    await _purge_templates(db, result["template_id"])


async def test_create_template_refuses_to_rewrite_an_existing_one(db):
    unique = f"Pytest Dupe {uuid.uuid4().hex[:8]}"
    first = await create_email_template(
        name=unique, subject="A", body="<p>a</p>", category="quotation"
    )
    assert first["status"] == "created"
    try:
        second = await create_email_template(
            name=unique.upper(), subject="B", body="<p>b</p>", category="holiday"
        )
        assert second["status"] == "duplicate"
        assert second["template_id"] == first["template_id"]

        from sqlalchemy import select

        from app.models.email_template import EmailTemplate

        row = (
            await db.execute(select(EmailTemplate).where(EmailTemplate.id == first["template_id"]))
        ).scalar_one()
        assert row.subject == "A", "the existing template must not be rewritten"
    finally:
        await _purge_templates(db, first["template_id"])


async def test_create_template_validates_its_enums(db):
    """A category the page cannot filter by is a template nobody can find."""
    assert "error" in await create_email_template(
        name="x", subject="s", body="b", category="newsletter"
    )
    assert "error" in await create_email_template(
        name="x", subject="s", body="b", category="cold_outreach", language="fr"
    )
    assert "error" in await create_email_template(name="  ", subject="s", body="b", category="holiday")
    assert "error" in await create_email_template(name="x", subject=" ", body="b", category="holiday")


async def test_create_template_dry_run_writes_nothing(db):
    unique = f"Pytest DryTpl {uuid.uuid4().hex[:8]}"
    result = await create_email_template(
        name=unique, subject="s", body="<p>b</p>", category="holiday", dry_run=True
    )
    assert result["status"] == "would_create"

    from sqlalchemy import select

    from app.models.email_template import EmailTemplate

    row = (
        await db.execute(select(EmailTemplate).where(EmailTemplate.name == unique))
    ).scalar_one_or_none()
    assert row is None


# --- notes, contacts, tasks -----------------------------------------------

async def test_add_note_lands_on_the_customer_timeline(db):
    """The note must show where a person looks for it: the detail page."""
    from sqlalchemy import select

    from app.models.customer import CustomerNote

    unique = f"Pytest note {uuid.uuid4().hex[:8]}"
    customer_id = await _any_customer_id(db)
    result = await add_customer_note(
        customer_id=customer_id, content=unique, note_type="quotation"
    )
    assert result["status"] == "created"
    try:
        row = (
            await db.execute(select(CustomerNote).where(CustomerNote.id == result["note_id"]))
        ).scalar_one()
        assert row.content == unique
        assert row.note_type == "quotation"

        again = await add_customer_note(
            customer_id=customer_id, content=unique.upper(), note_type="quotation"
        )
        assert again["status"] == "duplicate", "the same note twice is one note"
    finally:
        await _purge_notes(db, result["note_id"])


async def test_add_note_validates_type_and_customer(db):
    assert "error" in await add_customer_note(customer_id=1, content="x", note_type="gossip")
    assert "error" in await add_customer_note(customer_id=1, content="  ")
    assert "error" in await add_customer_note(customer_id=999_999, content="x")


async def test_add_contact_refuses_to_invent_and_never_overwrites(db):
    from sqlalchemy import select

    from app.models.customer import CustomerContact

    customer_id = await _any_customer_id(db)
    email = f"{uuid.uuid4().hex[:8]}@example.test"
    result = await add_customer_contact(
        customer_id=customer_id, name="Pytest Person", email=email
    )
    assert result["status"] == "created"
    try:
        again = await add_customer_contact(
            customer_id=customer_id, name="Someone Else", email=email
        )
        assert again["status"] == "duplicate"
        row = (
            await db.execute(select(CustomerContact).where(CustomerContact.id == result["contact_id"]))
        ).scalar_one()
        assert row.name == "Pytest Person", "the existing contact must not be rewritten"

        bad = await add_customer_contact(
            customer_id=customer_id, name="X", email="not-an-address"
        )
        assert "error" in bad
    finally:
        await _purge_contacts(db, result["contact_id"])


async def test_create_task_writes_a_row_the_tasks_page_lists(db):
    from sqlalchemy import select

    from app.models.task import Task

    unique = f"Pytest task {uuid.uuid4().hex[:8]}"
    customer_id = await _any_customer_id(db)
    result = await create_task(
        title=unique,
        description="check in about the sample",
        customer_id=customer_id,
        due_date="2027-03-01",
        priority="high",
        task_type="sample",
    )
    assert result["status"] == "created"
    assert result["priority"] == "high"
    try:
        row = (
            await db.execute(select(Task).where(Task.id == result["task_id"]))
        ).scalar_one()
        assert row.title == unique
        assert row.status == "pending"
        assert row.customer_id == customer_id
        assert row.due_date is not None

        listed = await list_tasks(customer_id=customer_id, status="pending")
        assert result["task_id"] in [t["task_id"] for t in listed["tasks"]]

        dupe = await create_task(title=unique.upper(), customer_id=customer_id)
        assert dupe["status"] == "duplicate"

        closed = await complete_task(task_id=result["task_id"])
        assert closed["status"] == "closed"
        assert closed["new_status"] == "completed"

        again = await complete_task(task_id=result["task_id"])
        assert again["status"] == "already_closed"
    finally:
        await _purge_tasks(db, result["task_id"])


async def test_create_task_refuses_an_unparseable_date(db):
    """A dropped date is a reminder that silently stopped being one."""
    result = await create_task(title="Pytest date", due_date="next tuesday")
    assert "error" in result
    assert "2026-10-27" in result["error"] or "ISO" in result["error"]


async def test_create_task_validates_its_enums(db):
    assert "error" in await create_task(title="x", priority="whenever")
    assert "error" in await create_task(title="x", task_type="nap")
    assert "error" in await create_task(title="  ")
    assert "error" in await create_task(title="x", customer_id=999_999)


# --- cleanup helpers ------------------------------------------------------

async def _purge_notes(db, note_id: int) -> None:
    from sqlalchemy import delete

    from app.models.customer import CustomerNote

    await db.execute(delete(CustomerNote).where(CustomerNote.id == note_id))
    await db.commit()


async def _purge_contacts(db, contact_id: int) -> None:
    from sqlalchemy import delete

    from app.models.customer import CustomerContact

    await db.execute(delete(CustomerContact).where(CustomerContact.id == contact_id))
    await db.commit()


async def _purge_tasks(db, task_id: int) -> None:
    from sqlalchemy import delete

    from app.models.task import Task

    await db.execute(delete(Task).where(Task.id == task_id))
    await db.commit()


async def _purge_templates(db, template_id: int) -> None:
    from sqlalchemy import delete

    from app.models.email_template import EmailTemplate

    await db.execute(delete(EmailTemplate).where(EmailTemplate.id == template_id))
    await db.commit()


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


async def _purge_one_email(db, email_id: int) -> None:
    from sqlalchemy import delete

    from app.models.email_log import EmailLog

    await db.execute(delete(EmailLog).where(EmailLog.id == email_id))
    await db.commit()


# --- list_email_templates -------------------------------------------------

async def test_list_email_templates_returns_the_seeded_templates(db):
    """Read-back is the point: a template you can only write is unusable."""
    result = await list_email_templates()

    assert result["count"] >= 2, "the demo seed ships two templates"
    names = [t["name"] for t in result["templates"]]
    assert "Cold Outreach - General" in names

    for tpl in result["templates"]:
        for key in ("id", "name", "category", "language", "subject", "placeholders_used"):
            assert key in tpl
        assert isinstance(tpl["placeholders_used"], list)


async def test_list_email_templates_filters_by_category(db):
    result = await list_email_templates(category="follow_up")
    assert result["count"] >= 1
    assert all(t["category"] == "follow_up" for t in result["templates"])

    # A category the page cannot filter by is refused, not silently ignored.
    assert "error" in await list_email_templates(category="newsletter")
    assert "error" in await list_email_templates(language="fr")


async def test_list_email_templates_searches_names_and_subjects(db):
    by_name = await list_email_templates(search="Cold Outreach")
    assert any("Cold Outreach" in t["name"] for t in by_name["templates"])

    by_subject = await list_email_templates(search="inquiry")
    assert by_subject["count"] >= 1, "the seeded follow-up subject contains 'inquiry'"


# --- render_email_template ------------------------------------------------

async def test_render_template_fills_the_variables_from_the_customer_record(db):
    from sqlalchemy import select

    from app.models.email_template import EmailTemplate

    row = (
        await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "Cold Outreach - General")
        )
    ).scalar_one()
    customer_id = await _any_customer_id(db)

    result = await render_email_template(template_id=row.id, customer_id=customer_id)

    assert "error" not in result
    assert result["subject"], "a rendered subject must not come back empty"
    # The company name came from the CRM, not from the caller.
    from app.models.customer import Customer

    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one()
    assert customer.company_name in result["subject"]
    assert "company_name" not in (result["unresolved_placeholders"] or [])
    assert "create_email_draft" in result["next_step"]


async def test_render_template_leaves_an_unknown_placeholder_standing(db):
    """A typo must stay visible; a silent gap is worse than no fill at all."""
    from sqlalchemy import select

    from app.models.email_template import EmailTemplate

    row = (
        await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "Cold Outreach - General")
        )
    ).scalar_one()

    result = await render_email_template(template_id=row.id, company_name="Acme Textiles")

    assert "Acme Textiles" in result["subject"]
    # contact_name was never supplied, so it is reported rather than blanked.
    assert "contact_name" in (result["unresolved_placeholders"] or [])
    assert "{contact_name}" in result["body"]


async def test_render_template_accepts_the_double_brace_dialect(db):
    """The editor teaches one dialect, Jinja understands the other."""
    unique = f"Pytest Braces {uuid.uuid4().hex[:8]}"
    created = await create_email_template(
        name=unique,
        subject="Hello {{ company_name }}",
        body="<p>Hi {contact_name}, we make {product_name}.</p>",
        category="follow_up",
    )
    assert created["status"] == "created"
    try:
        result = await render_email_template(
            template_id=created["template_id"],
            company_name="Acme Textiles",
            contact_name="Hans",
            product_name="gold thread",
        )
        assert result["subject"] == "Hello Acme Textiles"
        assert "Hans" in result["body"] and "gold thread" in result["body"]
        assert result["unresolved_placeholders"] is None
    finally:
        await _purge_templates(db, created["template_id"])


async def test_render_template_rejects_unknown_ids(db):
    assert "error" in await render_email_template(template_id=999999)
    from sqlalchemy import select

    from app.models.email_template import EmailTemplate

    row = (
        await db.execute(select(EmailTemplate).limit(1))
    ).scalar_one()
    assert "error" in await render_email_template(template_id=row.id, customer_id=999999)


# --- list_email_logs ------------------------------------------------------

async def test_list_email_logs_reports_the_pending_queue(db):
    """The desk, seen whole — what is waiting, for whom, and what it says."""
    customer_id = await _any_customer_id(db)
    created = await create_email_draft(
        customer_id=customer_id,
        subject=f"Queue check {uuid.uuid4().hex[:8]}",
        body="<p>Checking the queue view.</p>",
    )
    assert created["status"] == "draft_created"
    try:
        pending = await list_email_logs(status="pending")
        assert created["email_log_id"] in [e["id"] for e in pending["emails"]]
        assert pending["counts_by_status"].get("pending", 0) >= 1

        mine = await list_email_logs(customer_id=customer_id)
        assert all(e["customer_id"] == customer_id for e in mine["emails"])
        assert any(e["id"] == created["email_log_id"] for e in mine["emails"])
    finally:
        await _purge_emails(db, customer_id)


async def test_list_email_logs_validates_the_status(db):
    """A status outside the page's vocabulary is a filter that returns nothing."""
    assert "error" in await list_email_logs(status="draf")
    result = await list_email_logs()
    assert result["count"] >= 0
    for email in result["emails"]:
        for key in ("id", "customer_id", "to_email", "subject", "status"):
            assert key in email


# --- get_pipeline_stats ---------------------------------------------------

async def test_get_pipeline_stats_counts_every_stage(db):
    result = await get_pipeline_stats()

    assert "dashboard" in result and "stages" in result
    for key in ("total_customers", "open_rate", "reply_rate", "pending_tasks", "overdue_tasks"):
        assert key in result["dashboard"]
    # The funnel covers the whole vocabulary, so a stage with nobody in it is
    # reported as zero rather than missing.
    assert set(result["stages"]) == {
        "new", "contacted", "interested", "quoting",
        "sample", "ordering", "completed", "lost",
    }
    assert sum(result["stages"].values()) == result["total_customers_in_funnel"]
    assert result["total_customers_in_funnel"] >= result["dashboard"]["total_customers"]


# --- get_quality_report ---------------------------------------------------

async def test_get_quality_report_returns_operational_numbers(db):
    result = await get_quality_report(days=14)

    assert result["days"] == 14
    assert "runs" in result
    # Read-only: asking about the system must not write to it.
    assert "error" not in result


# --- update_customer_stage ------------------------------------------------

async def test_update_customer_stage_moves_only_the_stage(db):
    from sqlalchemy import select

    from app.models.customer import Customer

    unique = f"Pytest Stage Co {uuid.uuid4().hex[:8]}"
    created = await upsert_customer(
        company_name=unique, country="Germany", products="gold thread", score=42.0
    )
    assert created["status"] == "created"
    try:
        moved = await update_customer_stage(customer_id=created["customer_id"], stage="quoting")
        assert moved["status"] == "stage_updated"
        assert moved["stage_before"] == "new"
        assert moved["stage_after"] == "quoting"

        row = (
            await db.execute(select(Customer).where(Customer.id == created["customer_id"]))
        ).scalar_one()
        assert row.stage.value == "quoting"
        assert row.country == "Germany", "curated fields must not move with the stage"
        assert row.products == "gold thread"
        assert row.score == 42.0
    finally:
        await _delete_customer(db, created["customer_id"])


async def test_update_customer_stage_refuses_an_unknown_stage(db):
    created = await upsert_customer(company_name=f"Pytest Bad Stage {uuid.uuid4().hex[:8]}")
    try:
        result = await update_customer_stage(customer_id=created["customer_id"], stage="negotiating")
        assert "error" in result
        assert "quoting" in result["error"], "the error should name the real vocabulary"
    finally:
        await _delete_customer(db, created["customer_id"])


async def test_update_customer_stage_is_idempotent(db):
    created = await upsert_customer(company_name=f"Pytest Same Stage {uuid.uuid4().hex[:8]}")
    try:
        again = await update_customer_stage(customer_id=created["customer_id"], stage="new")
        assert again["status"] == "unchanged"
        assert again["stage"] == "new"
    finally:
        await _delete_customer(db, created["customer_id"])


async def test_update_customer_stage_dry_run_writes_nothing(db):
    from sqlalchemy import select

    from app.models.customer import Customer

    created = await upsert_customer(company_name=f"Pytest Stage Dry {uuid.uuid4().hex[:8]}")
    try:
        result = await update_customer_stage(
            customer_id=created["customer_id"], stage="lost", dry_run=True
        )
        assert result["status"] == "would_change"
        row = (
            await db.execute(select(Customer).where(Customer.id == created["customer_id"]))
        ).scalar_one()
        assert row.stage.value == "new", "dry_run must not write"
    finally:
        await _delete_customer(db, created["customer_id"])


async def test_update_customer_stage_unknown_customer(db):
    assert "error" in await update_customer_stage(customer_id=999999, stage="quoting")


# --- reschedule_task ------------------------------------------------------

async def test_reschedule_task_moves_only_the_due_date(db):
    created = await create_task(
        title=f"Pytest Reschedule {uuid.uuid4().hex[:8]}",
        description="send the revised quotation",
        due_date="2026-10-01",
        priority="high",
    )
    assert created["status"] == "created"
    try:
        moved = await reschedule_task(task_id=created["task_id"], due_date="2026-11-05")
        assert moved["status"] == "rescheduled"
        assert moved["due_date_before"] == "2026-10-01T00:00:00"
        assert moved["due_date_after"] == "2026-11-05T00:00:00"

        rows = await list_tasks(status="pending")
        task = next(t for t in rows["tasks"] if t["task_id"] == created["task_id"])
        assert task["due_date"] == "2026-11-05T00:00:00"
        assert task["title"] == created["title"], "the title must not be rewritten"
        assert task["priority"] == "high", "the priority must not be rewritten"
    finally:
        await _purge_tasks(db, created["task_id"])


async def test_reschedule_task_can_clear_the_date(db):
    created = await create_task(
        title=f"Pytest Clear Date {uuid.uuid4().hex[:8]}", due_date="2026-10-01"
    )
    try:
        moved = await reschedule_task(task_id=created["task_id"], due_date="")
        assert moved["status"] == "rescheduled"
        assert moved["due_date_after"] is None
    finally:
        await _purge_tasks(db, created["task_id"])


async def test_reschedule_task_parses_the_date_strictly(db):
    """A date the model meant and a date the system stored must agree."""
    created = await create_task(title=f"Pytest Bad Date {uuid.uuid4().hex[:8]}")
    try:
        result = await reschedule_task(task_id=created["task_id"], due_date="next tuesday")
        assert "error" in result
        assert "YYYY-MM-DD" in result["error"]
    finally:
        await _purge_tasks(db, created["task_id"])


async def test_reschedule_task_refuses_a_closed_task(db):
    created = await create_task(
        title=f"Pytest Closed Move {uuid.uuid4().hex[:8]}", due_date="2026-10-01"
    )
    try:
        await complete_task(task_id=created["task_id"])
        moved = await reschedule_task(task_id=created["task_id"], due_date="2026-12-01")
        assert moved["status"] == "already_closed"
        assert moved["current_status"] == "completed"
    finally:
        await _purge_tasks(db, created["task_id"])


async def test_reschedule_task_unchanged_when_the_date_is_the_same(db):
    created = await create_task(
        title=f"Pytest Same Date {uuid.uuid4().hex[:8]}", due_date="2026-10-01"
    )
    try:
        result = await reschedule_task(task_id=created["task_id"], due_date="2026-10-01")
        assert result["status"] == "unchanged"
    finally:
        await _purge_tasks(db, created["task_id"])


async def test_reschedule_task_unknown_id(db):
    assert "error" in await reschedule_task(task_id=999999, due_date="2026-11-01")


# --- reject_email_draft ---------------------------------------------------

async def test_reject_email_draft_keeps_the_row_and_marks_it_rejected(db):
    from sqlalchemy import select

    from app.models.email_log import EmailLog

    customer_id = await _any_customer_id(db)
    created = await create_email_draft(
        customer_id=customer_id,
        subject=f"Withdraw me {uuid.uuid4().hex[:8]}",
        body="<p>Second thoughts.</p>",
    )
    try:
        rejected = await reject_email_draft(
            email_id=created["email_log_id"], reason="quoting changed, resend later"
        )
        assert rejected["status"] == "rejected"
        assert rejected["reason_recorded"] == "quoting changed, resend later"

        row = (
            await db.execute(select(EmailLog).where(EmailLog.id == created["email_log_id"]))
        ).scalar_one()
        assert row.status.value == "rejected", "kept as a record, never deleted"
        assert row.subject == rejected["subject"], "the draft's words are not rewritten"

        again = await reject_email_draft(email_id=created["email_log_id"], reason="again")
        assert again["status"] == "already_rejected"
    finally:
        await _purge_one_email(db, created["email_log_id"])


async def test_reject_email_draft_refuses_anything_already_sent(db):
    from sqlalchemy import select

    from app.models.email_log import EmailLog, EmailStatus

    customer_id = await _any_customer_id(db)
    created = await create_email_draft(
        customer_id=customer_id,
        subject=f"Already sent {uuid.uuid4().hex[:8]}",
        body="<p>Gone.</p>",
    )
    try:
        await db.execute(
            select(EmailLog).where(EmailLog.id == created["email_log_id"])
        )
        row = (
            await db.execute(select(EmailLog).where(EmailLog.id == created["email_log_id"]))
        ).scalar_one()
        row.status = EmailStatus.SENT
        await db.commit()

        result = await reject_email_draft(email_id=created["email_log_id"], reason="too late")
        assert "error" in result
        assert "sent" in result["error"]
    finally:
        await _purge_one_email(db, created["email_log_id"])


async def test_reject_email_draft_dry_run_and_unknown_id(db):
    customer_id = await _any_customer_id(db)
    created = await create_email_draft(
        customer_id=customer_id,
        subject=f"Dry reject {uuid.uuid4().hex[:8]}",
        body="<p>Preview only.</p>",
    )
    try:
        preview = await reject_email_draft(email_id=created["email_log_id"], dry_run=True)
        assert preview["status"] == "would_reject"
        assert preview["dry_run"] is True
        assert "error" in await reject_email_draft(email_id=999999)
    finally:
        await _purge_one_email(db, created["email_log_id"])


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
