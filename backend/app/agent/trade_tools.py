"""CRM and prospecting tools — the agent's hands on real trade data.

Design rule
-----------
**Tools return facts. The model makes judgements.**

Compare with the code this replaces, ``agent_service.auto_lead_scanner``::

    relevance_score = 20
    if is_b2b_platform:        relevance_score += 30
    if buyer_keyword_in_text:  relevance_score += 20
    if product_keyword_match:  relevance_score += 20
    if emails_found:           relevance_score += 10
    if relevance_score >= 40:  keep()

Four magic numbers decided which prospects the business ever saw, forever.
Here the same work is split in two:

* ``search_prospects`` / ``inspect_website`` return raw candidates and raw
  page data — no scoring, no thresholds, nothing dropped.
* ``classify_prospect`` returns an explicit *rubric* plus the evidence. The
  model produces the score and the recommendation.

So every score in a run is traceable to a model turn that is recorded in the
trace, instead of to a constant nobody remembers choosing.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, or_, select

from app.models.customer import Customer, CustomerContact, CustomerNote
from app.models.email_log import EmailLog
from app.models.task import Task
from app.models.email_log import EmailStatus
from app.models.customer import CustomerSource, CustomerStage

from .tools import ToolRegistry, build_params_model, tool

logger = logging.getLogger(__name__)

# Every tool that returns a collection caps it, so a single call cannot blow up
# the model's context or hammer the database.
MAX_ROWS = 25
MAX_PAGE_CHARS = 3000

# Stages meaning "this relationship is over"; excluded unless asked for.
CLOSED_STAGES = {"completed", "lost"}


def _session_factory():
    """Resolve the session factory lazily.

    Late binding matters: tests point ``app.database.async_session`` at a test
    database, and a module-level import would freeze the production engine.
    """
    from app.database import async_session

    return async_session


def _status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    description=(
        "List customers in the CRM with optional filters. Returns factual rows "
        "(stage, score, country, last contact). Use this to see the pipeline."
    ),
    tags={"read", "crm"},
)
async def list_customers(
    stage: str = "",
    country: str = "",
    min_score: float = 0.0,
    search: str = "",
    limit: int = 20,
    include_closed: bool = False,
) -> dict:
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    stmt = select(Customer)
    filters = []

    if stage:
        wanted = [s.strip().lower() for s in stage.split(",") if s.strip()]
        filters.append(Customer.stage.in_(wanted))
    elif not include_closed:
        filters.append(Customer.stage.notin_(sorted(CLOSED_STAGES)))

    if country:
        filters.append(Customer.country.ilike(f"%{country.strip()}%"))
    if min_score:
        filters.append(Customer.score >= float(min_score))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Customer.company_name.ilike(pattern),
                Customer.products.ilike(pattern),
                Customer.industry.ilike(pattern),
                Customer.tags.ilike(pattern),
            )
        )
    if filters:
        stmt = stmt.where(*filters)

    stmt = stmt.order_by(Customer.score.desc(), Customer.id.asc()).limit(limit)

    async with _session_factory()() as db:
        rows = (await db.execute(stmt)).scalars().all()

    return {
        "count": len(rows),
        "filters": {
            "stage": stage or ("(open only)" if not include_closed else "(all)"),
            "country": country or None,
            "min_score": min_score or None,
            "search": search or None,
        },
        "customers": [
            {
                "id": c.id,
                "company_name": c.company_name,
                "country": c.country,
                "stage": _status(c.stage),
                "score": c.score,
                "products": c.products,
                "industry": c.industry,
                "website": c.website,
                "last_contacted_at": c.last_contacted_at.isoformat() if c.last_contacted_at else None,
                "next_follow_up_at": c.next_follow_up_at.isoformat() if c.next_follow_up_at else None,
            }
            for c in rows
        ],
    }


@tool(
    description=(
        "Full detail for one customer: contacts, notes and open task count. "
        "Pass the numeric customer id."
    ),
    tags={"read", "crm"},
)
async def get_customer(customer_id: int) -> dict:
    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        contacts = (
            await db.execute(
                select(CustomerContact).where(CustomerContact.customer_id == customer.id)
            )
        ).scalars().all()

        notes = (
            await db.execute(
                select(CustomerNote)
                .where(CustomerNote.customer_id == customer.id)
                .order_by(CustomerNote.created_at.desc())
                .limit(MAX_ROWS)
            )
        ).scalars().all()

        open_tasks = (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.customer_id == customer.id, Task.completed_at.is_(None))
            )
        ).scalar()

        return {
            "id": customer.id,
            "company_name": customer.company_name,
            "company_name_cn": customer.company_name_cn,
            "country": customer.country,
            "region": customer.region,
            "city": customer.city,
            "website": customer.website,
            "industry": customer.industry,
            "products": customer.products,
            "stage": _status(customer.stage),
            "source": _status(customer.source),
            "score": customer.score,
            "tags": customer.tags,
            "annual_import_value": customer.annual_import_value,
            "notes": customer.notes,
            "created_at": customer.created_at.isoformat() if customer.created_at else None,
            "updated_at": customer.updated_at.isoformat() if customer.updated_at else None,
            "last_contacted_at": customer.last_contacted_at.isoformat() if customer.last_contacted_at else None,
            "open_tasks": open_tasks,
            "contacts": [
                {
                    "id": ct.id,
                    "name": ct.name,
                    "title": ct.title,
                    "email": ct.email,
                    "phone": ct.phone,
                    "whatsapp": ct.whatsapp,
                    "is_primary": bool(ct.is_primary),
                }
                for ct in contacts
            ],
            "recent_notes": [
                {
                    "id": n.id,
                    "type": n.note_type,
                    "content": n.content,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in notes
            ],
        }


@tool(
    description=(
        "Every email logged against a customer, newest first, with open and "
        "reply state. Use this to understand how a conversation has gone."
    ),
    tags={"read", "crm"},
)
async def get_communication_history(customer_id: int, limit: int = 15) -> dict:
    limit = max(1, min(int(limit or 15), MAX_ROWS))
    async with _session_factory()() as db:
        rows = (
            await db.execute(
                select(EmailLog)
                .where(EmailLog.customer_id == int(customer_id))
                .order_by(EmailLog.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    if not rows:
        return {
            "customer_id": customer_id,
            "message_count": 0,
            "messages": [],
            "note": "no email records for this customer",
        }

    engaged = {"sent", "delivered", "opened", "replied"}
    messages = [
        {
            "id": m.id,
            "subject": m.subject,
            "status": _status(m.status),
            "to_email": m.to_email,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            "opened_at": m.opened_at.isoformat() if m.opened_at else None,
            "open_count": m.open_count or 0,
            "replied_at": m.replied_at.isoformat() if m.replied_at else None,
            "body_preview": (m.body or "")[:400],
        }
        for m in rows
    ]

    return {
        "customer_id": customer_id,
        "message_count": len(messages),
        "engaged_count": len([m for m in messages if m["status"] in engaged]),
        "replied_count": len([m for m in messages if m["status"] == "replied"]),
        "opened_count": len([m for m in messages if m["status"] == "opened"]),
        "pending_count": len([m for m in messages if m["status"] == "pending"]),
        "bounced_count": len([m for m in messages if m["status"] == "bounced"]),
        "last_message_at": messages[0]["created_at"],
        "messages": messages,
    }


@tool(
    description=(
        "Raw numeric signals for one customer: days since last contact, days in "
        "stage, counts of emails sent/opened/replied, never-contacted flag. "
        "Returns numbers, NOT a risk label — you decide what the numbers mean."
    ),
    tags={"read", "crm"},
)
async def get_customer_signals(customer_id: int) -> dict:
    """Numeric signals only, deliberately unlabelled.

    The code this replaces (``customer_churn_alerts``) hardcoded both the
    thresholds and the verdicts: three emails with no reply became "critical",
    fourteen days in quoting became "high", thirty days of silence became
    "medium". Those constants are somebody's opinion frozen at a point in time;
    they should be argued with, not inherited.
    """
    now = datetime.utcnow()
    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        emails = (
            await db.execute(
                select(EmailLog)
                .where(EmailLog.customer_id == customer.id)
                .order_by(EmailLog.created_at.desc())
            )
        ).scalars().all()

    sent_states = {"sent", "delivered", "opened", "replied"}
    last_touch = customer.last_contacted_at or (emails[0].created_at if emails else None)
    stage_ref = customer.updated_at or customer.created_at

    return {
        "customer_id": customer.id,
        "company_name": customer.company_name,
        "stage": _status(customer.stage),
        "country": customer.country,
        "score": customer.score,
        "age_days": (now - customer.created_at).days if customer.created_at else None,
        "days_since_last_contact": (now - last_touch).days if last_touch else None,
        "days_in_current_stage": (now - stage_ref).days if stage_ref else None,
        "emails_total": len(emails),
        "emails_sent": len([e for e in emails if _status(e.status) in sent_states]),
        "emails_replied": len([e for e in emails if _status(e.status) == "replied"]),
        "emails_opened": len([e for e in emails if _status(e.status) == "opened"]),
        "emails_pending": len([e for e in emails if _status(e.status) == "pending"]),
        "emails_bounced": len([e for e in emails if _status(e.status) == "bounced"]),
        "never_contacted": last_touch is None,
        "has_any_reply": any(_status(e.status) == "replied" for e in emails),
    }


@tool(
    description=(
        "Search the web for potential buyers of a product in a country. Returns "
        "unranked, unscored candidates with the snippet that matched. Inspect a "
        "candidate's website and judge it yourself."
    ),
    tags={"read", "prospect"},
)
async def search_prospects(keyword: str, country: str = "", max_results: int = 8) -> dict:
    """Return candidates. Nothing is filtered out and nothing is ranked.

    The previous implementation dropped every candidate scoring under 40 on a
    fixed heuristic. Here the agent decides, and a candidate it discards is
    visible in the trace along with its reasoning.
    """
    from ddgs import DDGS

    max_results = max(1, min(int(max_results or 8), 15))
    location = f" {country}" if country else ""
    queries = [
        f"{keyword} importer{location}",
        f"{keyword} buyer{location}",
        f"{keyword} wholesaler{location}",
    ]

    seen: set[str] = set()
    candidates: list[dict] = []
    errors: list[str] = []

    ddgs = DDGS()
    for query in queries:
        if len(candidates) >= max_results:
            break
        try:
            results = list(ddgs.text(query, max_results=max_results))
        except Exception as exc:  # noqa: BLE001 - a failed query is data, not a crash
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
            continue
        for item in results:
            href = item.get("href", "")
            if not href.startswith("http"):
                continue
            parts = href.split("/")
            domain = parts[2] if len(parts) > 2 else href
            if domain in seen:
                continue
            seen.add(domain)
            candidates.append(
                {
                    "company_name": (item.get("title") or "").split(" - ")[0].strip()[:150],
                    "website": href,
                    "domain": domain,
                    "snippet": (item.get("body") or "")[:300],
                    "search_keyword": keyword,
                    "search_country": country or None,
                }
            )
            if len(candidates) >= max_results:
                break

    return {
        "count": len(candidates),
        "queries_run": queries,
        "candidates": candidates,
        "errors": errors,
        "note": (
            "Unranked and unscored by design — it is your job to judge which of "
            "these are worth approaching."
        ),
    }


@tool(
    description=(
        "Fetch a company website and return its readable text, any email "
        "addresses and phone numbers found, and the page title. Use this to find "
        "out what a prospect actually does and how to reach them."
    ),
    tags={"read", "prospect"},
)
async def inspect_website(url: str, max_chars: int = MAX_PAGE_CHARS) -> dict:
    import requests
    from bs4 import BeautifulSoup

    max_chars = max(500, min(int(max_chars or MAX_PAGE_CHARS), 8000))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    try:
        response = requests.get(
            url,
            timeout=15,
            verify=False,  # noqa: S501 - many small trade sites have broken chains
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not fetch {url}: {type(exc).__name__}: {exc}", "url": url}

    raw = response.text or ""
    soup = BeautifulSoup(raw, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    text = soup.get_text(separator="\n", strip=True)

    return {
        "url": url,
        "status_code": response.status_code,
        "title": title[:300],
        "emails": _extract_emails(raw),
        "phones": _extract_phones(text),
        "text": text[:max_chars],
        "text_truncated": len(text) > max_chars,
        "text_total_chars": len(text),
    }


@tool(
    description=(
        "Get an explicit rubric for judging whether a prospect is worth "
        "approaching, together with the page text to judge. This tool does NOT "
        "score the prospect — it returns the criteria and the evidence so that "
        "you produce the score and the recommendation."
    ),
    tags={"read", "prospect"},
)
async def classify_prospect(page_text: str, target_products: str, extra_evidence: str = "") -> dict:
    """Return the rubric and the materials. No score is produced here, on purpose.

    This exists so the criteria stay explicit and comparable between runs,
    while the actual assessment remains a model decision that shows up in the
    trace with its reasoning.
    """
    return {
        "target_products": target_products,
        "rubric": [
            {
                "dimension": "buyer_intent",
                "question": "Do they buy or import goods like ours, rather than make them?",
                "look_for": [
                    "words such as import, importer, distributor, wholesaler, sourcing, procurement",
                    "a product range that complements rather than duplicates ours",
                    "manufacturing the exact same product (counts against)",
                ],
            },
            {
                "dimension": "product_fit",
                "question": f"Is anything they sell adjacent to '{target_products}'?",
                "look_for": [
                    "category pages, product listings or catalogue text",
                    "industry wording: textile, apparel, embroidery, craft, upholstery, trims",
                    "no related category anywhere (counts strongly against)",
                ],
            },
            {
                "dimension": "reachability",
                "question": "Is there a credible way to start a conversation?",
                "look_for": [
                    "a real contact email, not a template placeholder",
                    "a named person or a named department",
                    "a contact form is a weaker but usable fallback",
                ],
            },
            {
                "dimension": "scale",
                "question": "Does the site suggest enough volume to be worth a day's attention?",
                "look_for": [
                    "multiple locations, languages or markets",
                    "trade-show presence, certifications, years in business",
                    "a single-page placeholder site (counts against)",
                ],
            },
        ],
        "page_text": (page_text or "")[:MAX_PAGE_CHARS],
        "extra_evidence": extra_evidence or "",
        "required_output": {
            "company_summary": "one sentence describing what they actually do",
            "score": "integer 0-100, your judgement against the rubric above",
            "recommendation": "approach_now | approach_later | low_priority | not_a_fit",
            "reasoning": "cite the specific evidence you used, including what counts against them",
            "angle": "the single best opening angle, if you recommend approaching",
            "open_questions": ["anything you could not determine from this page"],
        },
    }


@tool(
    description=(
        "Add a prospect to the CRM. Will NOT overwrite an existing customer — "
        "reports 'skipped' instead. Set dry_run=true to preview without writing."
    ),
    tags={"write", "crm"},
)
async def upsert_customer(
    company_name: str,
    website: str = "",
    country: str = "",
    products: str = "",
    industry: str = "",
    score: float = 0.0,
    notes: str = "",
    dry_run: bool = False,
) -> dict:
    """Create a new prospect row. Never updates an existing one.

    Refusing to update matters: an agent silently rewriting data a human
    curated is far worse than an agent adding a duplicate you can delete.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return {"error": "company_name is required"}

    website = (website or "").strip()

    async with _session_factory()() as db:
        conditions = [Customer.company_name == company_name]
        if website:
            conditions.append(Customer.website == website)
        existing = (
            await db.execute(select(Customer).where(or_(*conditions)).limit(1))
        ).scalar_one_or_none()

        if existing is not None:
            return {
                "status": "skipped",
                "reason": "a customer with this name or website already exists",
                "existing_customer_id": existing.id,
                "existing_company_name": existing.company_name,
            }

        if dry_run:
            return {
                "status": "would_create",
                "dry_run": True,
                "company_name": company_name,
                "website": website or None,
                "country": country or None,
                "score": score,
            }

        row = Customer(
            company_name=company_name[:200],
            website=website or None,
            country=country or None,
            products=products or None,
            industry=industry or None,
            score=float(score or 0),
            notes=notes or None,
            source=CustomerSource.SCRAPER,
            stage=CustomerStage.NEW,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "status": "created",
            "customer_id": row.id,
            "company_name": row.company_name,
            "stage": _status(row.stage),
        }


@tool(
    description=(
        "Save an email draft against a customer for HUMAN REVIEW. This does not "
        "send anything — the draft lands in the pending queue for a person to "
        "check and send."
    ),
    tags={"write", "email"},
)
async def create_email_draft(
    customer_id: int,
    subject: str,
    body: str,
    to_email: str = "",
) -> dict:
    """Record a draft with status ``pending``.

    There is deliberately no send tool. This is the mechanism that keeps the
    agent inside its authority: it can prepare, a human dispatches.
    """
    if not (subject or "").strip():
        return {"error": "subject is required"}
    if not (body or "").strip():
        return {"error": "body is required"}

    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        recipient = (to_email or "").strip()
        if not recipient:
            contact = (
                await db.execute(
                    select(CustomerContact)
                    .where(
                        CustomerContact.customer_id == customer.id,
                        CustomerContact.is_primary == 1,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            recipient = (contact.email if contact else "") or ""

        if not recipient:
            return {
                "error": (
                    f"no email address on file for customer {customer_id}. Find one "
                    "first (inspect_website) rather than inventing one."
                )
            }

        draft = EmailLog(
            customer_id=customer.id,
            to_email=recipient,
            to_name=customer.company_name,
            subject=subject[:500],
            body=body,
            status=EmailStatus.PENDING,
        )
        db.add(draft)
        await db.commit()
        await db.refresh(draft)

        return {
            "status": "draft_created",
            "email_log_id": draft.id,
            "customer_id": customer.id,
            "to_email": recipient,
            "subject": draft.subject,
            "send_status": "pending — requires human review before sending",
        }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

# Tools the agent must never have. Kept as an explicit list so the boundary is
# testable rather than merely absent: tests/test_agent_tools_policy.py asserts
# no registered tool name matches these.
FORBIDDEN_TOOL_PATTERNS = ("send", "dispatch", "delete", "remove", "drop", "purge")


def build_trade_registry() -> ToolRegistry:
    """Every tool the agent may call.

    Enforced here by omission rather than by asking the model to behave: there
    is no send tool, no delete tool, and no tool that edits an existing
    customer or email log.
    """
    registry = ToolRegistry()
    for entry in (
        list_customers,
        get_customer,
        get_communication_history,
        get_customer_signals,
        search_prospects,
        inspect_website,
        classify_prospect,
        upsert_customer,
        create_email_draft,
    ):
        registry.register(entry)
    return registry
