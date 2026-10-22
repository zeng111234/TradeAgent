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

import asyncio
import time
import logging
import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, or_, select

from app.config import settings
from app.models.customer import Customer, CustomerContact, CustomerNote
from app.models.email_log import EmailLog
from app.models.email_template import EmailTemplate
from app.models.task import Task, TaskStatus
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

#: Ugly on purpose. Addresses are read out of raw page source, which means
#: ``sprite@2x.png`` from a stylesheet looks exactly like an address until you
#: look at the extension -- so the extension is checked.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NOT_AN_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|woff2?|ttf|ico)$", re.I)

#: Deliberately loose, then filtered by digit count: international trade pages
#: write numbers as +49 (0)40 123-456, 0049 40 123456, or just 040/123456.
_PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s\-().]{7,20}\d")


def _extract_emails(raw_html: str) -> list[str]:
    """Addresses found in a page's source, deduplicated, junk extensions dropped.

    These helpers were **called but never defined** in this module: the tool
    raised ``NameError`` on every page it successfully fetched, and nothing
    noticed because every test that touches it substitutes a canned response.
    """
    found: dict[str, str] = {}
    for match in _EMAIL_RE.findall(raw_html or ""):
        address = match.strip().strip(".").lower()
        if _NOT_AN_EMAIL.search(address):
            continue
        found.setdefault(address, match.strip())
    return list(found.values())[:5]


def _extract_phones(text: str) -> list[str]:
    """Phone numbers in a page's visible text, deduplicated.

    Filtered by digit count rather than by shape: a page is full of things that
    look like phone numbers (order ids, prices, years) and the only reliable
    distinction available without a library is that a real number has enough
    digits and not too many.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_RE.findall(text or ""):
        candidate = match.strip()
        digits = re.sub(r"\D", "", candidate)
        if not 8 <= len(digits) <= 15:
            continue
        key = digits.lstrip("0")
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found[:5]


def _session_factory():
    """Resolve the session factory lazily.

    Late binding matters: tests point ``app.database.async_session`` at a test
    database, and a module-level import would freeze the production engine.
    """
    from app.database import async_session

    return async_session


@tool(
    description=(
        "The configured customer policy's assessment of one customer: its risk "
        "level (critical/high/medium), the reasons it fired, the actions it "
        "suggests, and the thresholds it applied. This is the SAME policy that "
        "the churn and follow-up endpoints use, so the answer is consistent with "
        "what the dashboard shows. Use it to compare your own judgement against "
        "the house rules, and to explain a disagreement between them."
    ),
    tags={"read", "crm", "policy"},
)
async def get_customer_assessment(customer_id: int) -> dict:
    """The deterministic policy's verdict, plus the thresholds behind it.

    Why this exists: the agent had its own path to these signals
    (``get_customer_signals``, which returns raw numbers and no verdict) while the
    endpoints used hardcoded thresholds in ``agent_service``. Two judgement paths
    over the same data is a design smell — the agent could recommend chasing a
    customer the dashboard had not flagged, and neither could explain the other.

    Now there is one set of thresholds. The agent can still disagree with them,
    but it has to disagree with a number it can see and name, which is a much
    better position than disagreeing with a rule it never saw.

    The thresholds are returned alongside the verdict on purpose: a fixed rule
    presented without its constants invites the model to treat it as ground truth,
    and these numbers are opinions that may not fit the case in front of it.
    """
    from sqlalchemy import select

    from app.models.customer import Customer
    from app.models.email_log import EmailLog
    from app.services.follow_up_policy import (
        ChurnPolicy,
        FollowUpPolicy,
        assess_churn,
        assess_follow_up,
        days_between,
        derive_thread_state,
    )

    now = datetime.utcnow()
    churn_policy = ChurnPolicy()
    follow_policy = FollowUpPolicy()

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

    stage = _status(customer.stage)
    sent_states = {"sent", "delivered", "opened", "replied"}
    last_email = emails[0] if emails else None
    last_contact = customer.last_contacted_at or (last_email.created_at if last_email else None)
    stage_ref = customer.updated_at or customer.created_at

    thread = derive_thread_state(emails, now)

    churn = assess_churn(
        stage=stage,
        created_days_ago=days_between(now, customer.created_at),
        days_since_contact=days_between(now, last_contact),
        days_in_stage=days_between(now, stage_ref),
        emails_sent=len([e for e in emails if _status(e.status) in sent_states]),
        emails_replied=len([e for e in emails if _status(e.status) == "replied"]),
        last_email_unopened=bool(last_email) and _status(last_email.status) == "sent",
        last_email_days_ago=days_between(now, last_email.created_at) if last_email else None,
        thread=thread,
        policy=churn_policy,
    )

    follow = assess_follow_up(
        stage=stage,
        days_since_last_email=days_between(now, last_email.created_at) if last_email else None,
        has_any_email=last_email is not None,
        thread=thread,
        policy=follow_policy,
    )

    return {
        "customer_id": customer.id,
        "company_name": customer.company_name,
        "stage": stage,
        "risk": {
            "level": churn.level,
            "at_risk": churn.at_risk,
            "reasons": churn.reasons,
            "suggested_actions": churn.actions,
        },
        "follow_up": (
            {"priority": follow[0], "reason": follow[1], "suggested_action": follow[2]}
            if follow else None
        ),
        # Who spoke last, and whether an answer is owed. This is the field to
        # read before concluding anything from a count of emails: three sent and
        # zero replies looks identical whether the customer is ignoring you or
        # you are ignoring them.
        "conversation": thread.as_dict(),
        "inputs": {
            "days_since_contact": days_between(now, last_contact),
            "days_in_stage": days_between(now, stage_ref),
            "age_days": days_between(now, customer.created_at),
            "emails_total": len(emails),
            "emails_sent": len([e for e in emails if _status(e.status) in sent_states]),
            "emails_replied": len([e for e in emails if _status(e.status) == "replied"]),
            "emails_awaiting_reply": thread.awaiting_reply,
            "emails_superseded_by_a_reply": thread.superseded,
            "they_wrote_last": thread.they_wrote_last,
            "never_contacted_per_log": last_contact is None,
        },
        "thresholds_applied": {
            "no_reply_critical_emails": churn_policy.no_reply_critical_emails,
            "silent_warm_after_days": churn_policy.silent_warm_after_days,
            "silent_contacted_after_days": churn_policy.silent_contacted_after_days,
            "stuck_quoting_after_days": churn_policy.stuck_quoting_after_days,
            "untouched_new_after_days": churn_policy.untouched_new_after_days,
            "reply_owed_critical_after_days": churn_policy.reply_owed_critical_after_days,
            "follow_up_no_reply_after_days": follow_policy.contacted_no_reply_after_days,
            "follow_up_quoting_is_always": follow_policy.quoting_always_priority,
        },
        "note": (
            "This is the configured policy, not ground truth. The thresholds above "
            "are conventions that may not fit this case. Where you disagree, say so "
            "and give your reason — and note that the churn rule and the follow-up "
            "rule answer different questions, so they can disagree with each other "
            "about the same customer. Read `conversation` before reading the counts: "
            "`awaiting_reply` counts emails sent after their last reply, and "
            "`superseded` counts the ones their reply overtook, which are not "
            "evidence that they are ignoring you."
        ),
    }


def _status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    description=(
        "List customers in the CRM with optional filters. Returns factual rows "
        "(stage, score, country, last contact). Use this to see the pipeline. "
        "Read `total` for how many customers match the filter: `count` is only "
        "how many rows this page returned, so a small limit makes it small. "
        "Also note the default excludes closed stages — set include_closed=true "
        "to count the whole book."
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

    # The size of the book, counted apart from the page that was asked for.
    # ``count`` used to be both, which made ``limit=1`` answer ``count: 1``
    # about a five-customer CRM; a live run read that as "1 customer" and only
    # got the right number because it had also called get_pipeline_stats.
    count_stmt = select(func.count()).select_from(Customer)
    if filters:
        count_stmt = count_stmt.where(*filters)

    async with _session_factory()() as db:
        total = (await db.execute(count_stmt)).scalar_one()
        rows = (await db.execute(stmt)).scalars().all()

    result = {
        "total": int(total or 0),
        "returned": len(rows),
        # Kept because it was already in the contract; ``total`` is the field
        # to trust. They differ exactly when the page was truncated.
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
    if len(rows) < result["total"]:
        # Said out loud, because a truncated page that reports only what it
        # returned is how "5 customers" becomes "1 customer".
        result["note"] = (
            f"this page holds {len(rows)} of {result['total']} matching customers; "
            "raise limit to see the rest"
        )
    return result


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
    from app.services.follow_up_policy import derive_thread_state

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
        "conversation": derive_thread_state(list(emails), now).as_dict(),
    }


@tool(
    description=(
        "Today's follow-up plan for the whole book: one entry per customer, each "
        "with the next action (kind), when it is due, how many emails have gone "
        "unanswered, and the evidence it was computed from. Read this before "
        "deciding who to contact: it is the schedule, and it is deterministic. "
        "The judgement about how to approach a given customer is yours."
    ),
    tags={"read", "crm", "plan"},
)
async def get_follow_up_plan(due_only: bool = False, limit: int = 10) -> dict:
    """The plan, read-only.

    Deliberately does not commit anything: reading a plan must not change it, so
    this calls the planner with ``persist=False``. The scheduled job is what
    writes ``next_follow_up_at``.

    ``due_only`` narrows to what is due now. The full view includes entries
    scheduled forward, which is where the answer to "what happens next" lives.
    """
    from app.services.agent_service import daily_follow_up_intelligence

    async with _session_factory()() as db:
        plan = await daily_follow_up_intelligence(
            db_session=db, due_only=due_only, persist=False
        )

    actions = plan.get("actions", [])[: max(1, min(int(limit), 50))]
    return {
        "as_of": plan.get("as_of"),
        "due_only": due_only,
        "total_actions": plan.get("total_actions", 0),
        "deferred": plan.get("deferred", 0),
        "by_kind": plan.get("by_kind", {}),
        "actions": actions,
        "note": (
            "`kind` is a closed set: answer_them (they wrote last, we owe them "
            "one), fix_delivery (the last email never arrived), send_follow_up, "
            "change_channel (enough emails with no answer), review (parked). "
            "`attempt` counts emails sent since the customer last spoke. Where "
            "you disagree with an entry, say so and give your reason."
        ),
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

    The search itself is blocking network I/O, so it runs on a worker thread.
    An ``async def`` whose body never awaits does not yield to the event loop:
    it freezes the process. Measured, three blocking page fetches delayed every
    other coroutine by 2950 ms; the same work on a thread delayed them by 12 ms
    and took no longer.
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

    def _search(query: str) -> list[dict]:
        """Runs on a worker thread; DDGS is synchronous and slow."""
        return list(DDGS().text(query, max_results=max_results))

    for query in queries:
        if len(candidates) >= max_results:
            break
        try:
            results = await asyncio.to_thread(_search, query)
        except Exception as exc:  # noqa: BLE001 - a failed query is data, not a crash
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
            continue
        usable = 0
        for item in results:
            # ``isinstance`` before ``.get``: a provider that stops returning
            # dicts must be reported as a shape change, not raise AttributeError
            # from inside the loop.
            href = item.get("href", "") if isinstance(item, dict) else ""
            if not href.startswith("http"):
                continue
            usable += 1
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

        if results and usable == 0:
            # Results arrived, and not one had a usable 'href'. An empty market
            # and a renamed field produce the same zero candidates; only the
            # raw count tells them apart. Without this the scan would report a
            # quiet day while the provider's schema had changed underneath it.
            errors.append(
                f"{query}: {len(results)} result(s) came back but none had a "
                "usable 'href' field -- the search provider's response shape "
                "may have changed"
            )

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
    """Fetch and read one page.

    The fetch **and the parse** run on a worker thread. ``requests.get`` is
    blocking with a 15-second timeout, and lxml parsing is CPU-bound; either one
    inside an ``async def`` freezes every other coroutine in the process for its
    whole duration. Measured, three of these delayed unrelated work by 2950 ms;
    on a thread, by 12 ms, with no increase in wall-clock time.
    """
    from bs4 import BeautifulSoup

    from app.url_guard import BlockedURL, fetch_public

    max_chars = max(500, min(int(max_chars or MAX_PAGE_CHARS), 8000))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    def _fetch() -> tuple[int, str]:
        """Blocking network call plus the parse. Runs on a worker thread.

        Two attempts, not one. A reset connection or a 503 from a small trade site
        is a page that a single second would have fetched -- before this, it was
        simply a page the agent never read, reported as a tool failure. Bounded by
        ``PAGE_FETCH_SECONDS`` and by the tool's own timeout, so the retry cannot
        turn a 15-second tool into a 40-second one.

        Deliberately not retried: a 404 or a 403. Those are answers, not accidents.
        """
        from app.timeouts import PAGE_FETCH_SECONDS
        from app.retry import classify_http_status, backoff_seconds

        deadline = time.monotonic() + PAGE_FETCH_SECONDS

        for attempt in (1, 2):
            try:
                response = fetch_public(
                    url,
                    timeout=PAGE_FETCH_SECONDS,
                    verify=settings.WEBSITE_TLS_VERIFY,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        )
                    },
                )
            except BlockedURL:
                # Not a transport accident: retrying an address the guard
                # refused would only ask the same question twice.
                raise
            except Exception:  # noqa: BLE001 - transport failures are the case here
                # requests raises rather than returning a status for connection
                # resets, DNS hiccups and timeouts. Those are exactly the ones
                # worth one more try; there is no status to classify.
                if attempt == 2:
                    raise
                time.sleep(backoff_seconds(1, cap=1.0))
                continue

            decision = classify_http_status(response.status_code)
            if decision.retryable and attempt == 1:
                if time.monotonic() + decision.wait_seconds < deadline:
                    time.sleep(decision.wait_seconds)
                    continue
            return response.status_code, response.text or ""

        raise RuntimeError("fetch failed without an error")  # pragma: no cover

    try:
        status_code, raw = await asyncio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not fetch {url}: {type(exc).__name__}: {exc}", "url": url}

    def _parse() -> tuple[str, str]:
        soup = BeautifulSoup(raw, "lxml")
        page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
        return page_title, soup.get_text(separator="\n", strip=True)

    title, text = await asyncio.to_thread(_parse)

    return {
        "url": url,
        "status_code": status_code,
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
        # judgement-boundary: contract -- this dict is not an assessment, it is
        # the *shape of the answer we are asking the model for*. The constants
        # are descriptions of what the model must produce, which is the opposite
        # of deterministic code answering a judgement question. The one guard in
        # tests/test_judgement_boundary.py allows a constant judgement value only
        # when it means "not assessed", or when the statement carries this mark.
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

    **Duplicate drafts are refused.** Two pending drafts for the same customer
    with the same subject are not two pieces of work -- they are one piece of
    work listed twice, and a human working down the list sends the customer the
    same email twice. Re-running an analysis is easy to do (a retry, a second
    click, the same goal twice in a day) and used to leave exactly that. The
    second call now returns ``duplicate`` with the id of the draft already
    waiting, so the agent can see that its work is already on the desk.

    A *different* subject is still allowed: answering a question and chasing a
    quote are different emails, and only the human knows which one this is.
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

        # Still waiting on a human, same customer, same subject: this is the
        # same piece of work.
        waiting = (
            await db.execute(
                select(EmailLog)
                .where(
                    EmailLog.customer_id == customer.id,
                    EmailLog.status == EmailStatus.PENDING,
                    func.lower(EmailLog.subject) == subject.strip().lower(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if waiting is not None:
            return {
                "status": "duplicate",
                "email_log_id": waiting.id,
                "customer_id": customer.id,
                "subject": waiting.subject,
                "reason": (
                    "a draft with this subject is already waiting for review for "
                    "this customer. Nothing was created -- sending both would "
                    "email them twice. If this is genuinely a different email, "
                    "use a different subject."
                ),
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

#: Placeholders the template editor offers and the send path fills in. A
#: template is a reusable artefact a person will send from, so a placeholder
#: outside this set is a typo that would otherwise travel all the way to a
#: customer's inbox as literal text.
TEMPLATE_VARIABLES = (
    "company_name",
    "contact_name",
    "contact_title",
    "product_name",
    "country",
    "industry",
)

#: The Templates page filters on exactly these, and its cards are tagged from
#: them. Free text would still save, but the template would then be invisible
#: to anyone filtering the page, which is the same as lost.
TEMPLATE_CATEGORIES = ("cold_outreach", "follow_up", "quotation", "holiday")
TEMPLATE_LANGUAGES = ("en", "zh", "es")

_PLACEHOLDER_RE = re.compile(r"\{\s*(\w+)\s*\}")


def _placeholders(text: str) -> list[str]:
    """Placeholder names in a template, in order of first appearance."""
    used: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        token = match.group(1)
        if token not in used:
            used.append(token)
    return used


def _unresolved(text: str) -> list[str]:
    """Placeholder names still standing after a render.

    ``render_template`` leaves an unknown name alone on purpose: an untouched
    ``{comapny_name}`` is a typo worth catching, while a silent gap is not. So
    the leftovers are the interesting output, and they are reported by name.
    """
    found: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        start, end = match.span()
        # A match that is part of Jinja's own ``{{ name }}`` is not a leftover
        # of this dialect; the render pass either filled it or left it whole.
        if start > 0 and text[start - 1] == "{":
            continue
        if end < len(text) and text[end] == "}":
            continue
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


@tool(
    description=(
        "Save a reusable email template. It appears on the 邮件模板 (Templates) "
        "page alongside the existing ones, ready for a person to send from. "
        "Use {company_name}, {contact_name}, {contact_title}, {product_name}, "
        "{country}, {industry} as placeholders — they are filled per recipient "
        "when the mail goes out. This does not send anything and does not "
        "create a draft; it only stores the template."
    ),
    tags={"write", "email"},
)
async def create_email_template(
    name: str,
    subject: str,
    body: str,
    category: str = "",
    language: str = "en",
    dry_run: bool = False,
) -> dict:
    """Store a template the team can send from.

    Like ``upsert_customer``, an existing name is never rewritten. A template a
    person has already edited is theirs; silently replacing it would lose their
    wording while looking like a success. The second call reports the row that
    already exists instead.

    Unlike a draft, this is meant to be reused, so an empty category is refused
    rather than allowed: the Templates page groups and filters on category, and
    a template filed under nothing is one nobody can find again.
    """
    name = (name or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    category = (category or "").strip().lower()
    language = (language or "").strip().lower()

    if not name:
        return {"error": "name is required"}
    if not subject:
        return {"error": "subject is required"}
    if not body:
        return {"error": "body is required"}
    if len(name) > 200:
        return {"error": "name is too long (200 characters max)"}
    if len(subject) > 500:
        return {"error": "subject is too long (500 characters max)"}
    if category not in TEMPLATE_CATEGORIES:
        return {
            "error": (
                f"unknown category {category!r}. The Templates page filters on "
                f"exactly these: {', '.join(TEMPLATE_CATEGORIES)}"
            )
        }
    if language not in TEMPLATE_LANGUAGES:
        return {
            "error": (
                f"unknown language {language!r}. Supported: "
                f"{', '.join(TEMPLATE_LANGUAGES)}"
            )
        }

    # Placeholders are checked against the set the send path can fill. The
    # check is advisory rather than fatal -- a template may legitimately carry
    # a fixed brand phrase in braces -- but a typo here is invisible until a
    # customer reads it, which is late to find out.
    used = _placeholders(f"{subject}\n{body}")
    unknown = [t for t in used if t not in TEMPLATE_VARIABLES]

    async with _session_factory()() as db:
        existing = (
            await db.execute(
                select(EmailTemplate).where(func.lower(EmailTemplate.name) == name.lower()).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "duplicate",
                "template_id": existing.id,
                "name": existing.name,
                "reason": (
                    "a template with this name already exists. Nothing was "
                    "changed — editing someone's template is not this tool's "
                    "job. Use a different name if this is genuinely a new one."
                ),
                "unknown_placeholders": unknown or None,
            }

        if dry_run:
            return {
                "status": "would_create",
                "dry_run": True,
                "name": name,
                "subject": subject,
                "category": category,
                "language": language,
                "placeholders_used": used,
                "unknown_placeholders": unknown or None,
            }

        row = EmailTemplate(
            name=name[:200],
            subject=subject[:500],
            body=body,
            language=language,
            category=category,
            is_ai_generated=1,
            use_count=0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        return {
            "status": "created",
            "template_id": row.id,
            "name": row.name,
            "category": row.category,
            "language": row.language,
            "is_ai_generated": True,
            "placeholders_used": used,
            "unknown_placeholders": unknown or None,
            "visible_in": "邮件模板 (Templates) page",
        }


# ------------------------------------------------------- notes and tasks ---
# The rest of the pages a person writes to. All additive: a note, a contact,
# a task, and the one status change that closes a task. Nothing here rewrites
# what somebody else wrote, and nothing here reaches a customer.

#: Note kinds the customer page tags from. Free text would save and then read
#: as an unlabelled blob in the timeline.
NOTE_TYPES = ("general", "email", "call", "meeting", "quotation")

#: The Tasks page filters and tags on exactly these two vocabularies.
TASK_TYPES = ("follow_up", "call", "email", "meeting", "quotation", "sample")
TASK_PRIORITIES = ("low", "medium", "high", "urgent")


def _parse_due(raw: str) -> tuple[Optional[datetime], Optional[str]]:
    """``2026-10-27`` or an ISO datetime, or an error naming the problem.

    A date the model meant and a date the system stored must agree, and an
    unparseable one must not silently become "no due date" -- that is how a
    reminder quietly stops being a reminder.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        return datetime.fromisoformat(raw), None
    except ValueError:
        return None, (
            f"due_date {raw!r} is not a date. Write it as YYYY-MM-DD or a full "
            "ISO datetime such as 2026-10-27T09:00."
        )


@tool(
    description=(
        "Record a note on a customer's timeline. It shows on the customer's "
        "detail page under notes, for whoever opens that customer next. Use it "
        "for what was agreed, what they asked for, or anything the next person "
        "needs. note_type is one of: general, email, call, meeting, quotation. "
        "This stores the note only — it notifies nobody. The same content twice "
        "on one customer is refused as a duplicate rather than added again."
    ),
    tags={"write", "crm"},
)
async def add_customer_note(
    customer_id: int,
    content: str,
    note_type: str = "general",
    dry_run: bool = False,
) -> dict:
    """Append to the timeline. Notes are history, so nothing is rewritten."""
    content = (content or "").strip()
    note_type = (note_type or "general").strip().lower()

    if not content:
        return {"error": "content is required"}
    if note_type not in NOTE_TYPES:
        return {
            "error": (
                f"unknown note_type {note_type!r}. Supported: "
                f"{', '.join(NOTE_TYPES)}"
            )
        }

    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        # The same sentence twice in a timeline is one event listed twice.
        # Re-running a goal is ordinary; a duplicate note is not two memories.
        twin = (
            await db.execute(
                select(CustomerNote).where(
                    CustomerNote.customer_id == customer.id,
                    func.lower(CustomerNote.content) == content.lower(),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if twin is not None:
            return {
                "status": "duplicate",
                "note_id": twin.id,
                "customer_id": customer.id,
                "reason": (
                    "this note already exists on the customer. Nothing was "
                    "added — the timeline would show the same entry twice."
                ),
            }

        if dry_run:
            return {
                "status": "would_create",
                "dry_run": True,
                "customer_id": customer.id,
                "company_name": customer.company_name,
                "note_type": note_type,
                "content": content,
            }

        row = CustomerNote(
            customer_id=customer.id,
            content=content,
            note_type=note_type,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "status": "created",
            "note_id": row.id,
            "customer_id": customer.id,
            "company_name": customer.company_name,
            "note_type": row.note_type,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


@tool(
    description=(
        "Add a contact person to a customer. It shows on the customer's detail "
        "page under contacts. Give the name and whatever you actually have — "
        "title, email, phone. Never invent an address or a number: leave the "
        "field empty and say so. Set is_primary=true only if this is the person "
        "to write to. An existing contact with the same email (or the same "
        "name) is reported as a duplicate and left untouched."
    ),
    tags={"write", "crm"},
)
async def add_customer_contact(
    customer_id: int,
    name: str,
    title: str = "",
    email: str = "",
    phone: str = "",
    whatsapp: str = "",
    linkedin: str = "",
    is_primary: bool = False,
    dry_run: bool = False,
) -> dict:
    """Add a person, never rewrite one.

    A contact is somebody's real counterpart. Overwriting their address with
    one scraped from a page would be worse than not having it, so an existing
    record is reported instead of updated.
    """
    name = (name or "").strip()
    email = (email or "").strip()
    if not name:
        return {"error": "name is required"}
    if email and "@" not in email:
        return {"error": f"{email!r} does not look like an email address"}

    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        conditions = [CustomerContact.customer_id == customer.id]
        if email:
            conditions.append(func.lower(CustomerContact.email) == email.lower())
        else:
            conditions.append(func.lower(CustomerContact.name) == name.lower())
        existing = (
            await db.execute(select(CustomerContact).where(*conditions).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "duplicate",
                "contact_id": existing.id,
                "customer_id": customer.id,
                "existing_name": existing.name,
                "existing_email": existing.email,
                "reason": (
                    "this person is already on the customer. Nothing was "
                    "changed — overwriting a contact a person saved is not "
                    "this tool's job."
                ),
            }

        if dry_run:
            return {
                "status": "would_create",
                "dry_run": True,
                "customer_id": customer.id,
                "company_name": customer.company_name,
                "name": name,
                "email": email or None,
            }

        row = CustomerContact(
            customer_id=customer.id,
            name=name[:100],
            title=(title or "").strip()[:100] or None,
            email=email or None,
            phone=(phone or "").strip()[:50] or None,
            whatsapp=(whatsapp or "").strip()[:50] or None,
            linkedin=(linkedin or "").strip()[:300] or None,
            is_primary=1 if is_primary else 0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "status": "created",
            "contact_id": row.id,
            "customer_id": customer.id,
            "company_name": customer.company_name,
            "name": row.name,
            "email": row.email,
            "is_primary": bool(row.is_primary),
        }


@tool(
    description=(
        "Create a task on the 待办事项 (Tasks) page — a reminder or a piece of "
        "work to come back to. Give a title, when it is due (YYYY-MM-DD or an "
        "ISO datetime), and customer_id if it is about one customer. "
        "priority: low | medium | high | urgent. "
        "task_type: follow_up | call | email | meeting | quotation | sample. "
        "This only stores the task; it does not remind anyone on its own. An "
        "open task with the same title for the same customer is reported as a "
        "duplicate rather than added again."
    ),
    tags={"write", "tasks"},
)
async def create_task(
    title: str,
    description: str = "",
    customer_id: int = 0,
    due_date: str = "",
    priority: str = "medium",
    task_type: str = "follow_up",
    dry_run: bool = False,
) -> dict:
    """Put work on the list a person already works from.

    A task with no due date is a note with delusions: it lands on the page
    outside every date filter, which is where reminders go to be forgotten.
    The date is therefore parsed strictly rather than dropped on error.
    """
    title = (title or "").strip()
    description = (description or "").strip()
    priority = (priority or "medium").strip().lower()
    task_type = (task_type or "follow_up").strip().lower()

    if not title:
        return {"error": "title is required"}
    if len(title) > 300:
        return {"error": "title is too long (300 characters max)"}
    if priority not in TASK_PRIORITIES:
        return {
            "error": (
                f"unknown priority {priority!r}. Supported: "
                f"{', '.join(TASK_PRIORITIES)}"
            )
        }
    if task_type not in TASK_TYPES:
        return {
            "error": (
                f"unknown task_type {task_type!r}. Supported: "
                f"{', '.join(TASK_TYPES)}"
            )
        }

    due, due_error = _parse_due(due_date)
    if due_error:
        return {"error": due_error}

    async with _session_factory()() as db:
        customer = None
        if customer_id:
            customer = (
                await db.execute(select(Customer).where(Customer.id == int(customer_id)))
            ).scalar_one_or_none()
            if customer is None:
                return {"error": f"no customer with id {customer_id}"}

        # Still open, same customer, same title: one piece of work, listed
        # twice. Closing a duplicate is worse than never making it, because
        # whoever works the list spends real minutes finding that out.
        conditions = [
            func.lower(Task.title) == title.lower(),
            Task.status.in_(("pending", "in_progress")),
        ]
        if customer is not None:
            conditions.append(Task.customer_id == customer.id)
        else:
            conditions.append(Task.customer_id.is_(None))
        twin = (
            await db.execute(select(Task).where(*conditions).limit(1))
        ).scalar_one_or_none()
        if twin is not None:
            return {
                "status": "duplicate",
                "task_id": twin.id,
                "reason": (
                    "an open task with this title already exists. Nothing was "
                    "added. Give it a different title if this is genuinely "
                    "separate work."
                ),
            }

        if dry_run:
            return {
                "status": "would_create",
                "dry_run": True,
                "title": title,
                "due_date": due.isoformat() if due else None,
                "priority": priority,
                "task_type": task_type,
                "customer_id": customer.id if customer else None,
            }

        row = Task(
            title=title[:300],
            description=description or None,
            customer_id=customer.id if customer else None,
            due_date=due,
            priority=priority,
            task_type=task_type,
            status="pending",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "status": "created",
            "task_id": row.id,
            "title": row.title,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "priority": row.priority if isinstance(row.priority, str) else row.priority.value,
            "task_type": row.task_type,
            "customer_id": row.customer_id,
            "company_name": customer.company_name if customer else None,
            "visible_in": "待办事项 (Tasks) page",
        }


@tool(
    description=(
        "Mark a task completed, or cancel it. Only the status changes: the "
        "title, due date and description stay exactly as they are. Use it when "
        "you tell me something is finished, or that it is no longer needed."
    ),
    tags={"write", "tasks"},
)
async def complete_task(
    task_id: int,
    cancelled: bool = False,
    dry_run: bool = False,
) -> dict:
    """Close what is already on the list.

    Narrow on purpose: no title, no date, no description can be edited here.
    Flipping a status is bookkeeping anybody can check; rewriting what a task
    says is editing somebody's plan.
    """
    async with _session_factory()() as db:
        task = (
            await db.execute(select(Task).where(Task.id == int(task_id)))
        ).scalar_one_or_none()
        if task is None:
            return {"error": f"no task with id {task_id}"}

        current = task.status if isinstance(task.status, str) else task.status.value
        if current in ("completed", "cancelled"):
            return {
                "status": "already_closed",
                "task_id": task.id,
                "current_status": current,
                "reason": "this task is already closed. Nothing was changed.",
            }

        new_status = "cancelled" if cancelled else "completed"
        if dry_run:
            return {
                "status": "would_close",
                "dry_run": True,
                "task_id": task.id,
                "title": task.title,
                "current_status": current,
                "new_status": new_status,
            }

        task.status = new_status
        task.completed_at = datetime.utcnow()
        await db.commit()
        return {
            "status": "closed",
            "task_id": task.id,
            "title": task.title,
            "new_status": new_status,
        }


#: The only values ``Task.status`` takes. Named here so the filter can say
#: "that is not a status" instead of quietly answering "none": asked for
#: ``status="open"`` -- the natural English word for a task nobody has closed
#: -- the query matched nothing and returned ``count: 0``, which reads exactly
#: like a true empty list. A model that only made that one call would report
#: "there are no open tasks" about a book with three of them.
TASK_STATUSES = tuple(s.value for s in TaskStatus)


@tool(
    description=(
        "List tasks from the 待办事项 (Tasks) page — what is still pending, what "
        "is due, what was already closed. Read-only. Filter by customer_id, by "
        "status (exactly one of: pending, in_progress, completed, cancelled — "
        "an unfinished task is 'pending'), or set due_only=true for what is due "
        "now. An unrecognised status is refused rather than answered empty."
    ),
    tags={"read", "tasks"},
)
async def list_tasks(
    customer_id: int = 0,
    status: str = "",
    due_only: bool = False,
    limit: int = 10,
) -> dict:
    """Read the list before adding to it.

    Without this the model would create a reminder it cannot see, and a second
    goal about the same customer would stack a second one on top.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    status = (status or "").strip().lower()

    if status and status not in TASK_STATUSES:
        return {
            "error": (
                f"unknown status {status!r}, so nothing was filtered. The Tasks "
                f"page stores exactly these: {', '.join(TASK_STATUSES)}. A task "
                "nobody has closed is 'pending', not 'open' — call again with "
                "one of the values above."
            ),
            "valid_statuses": list(TASK_STATUSES),
        }

    conditions = []
    if customer_id:
        conditions.append(Task.customer_id == int(customer_id))
    if status:
        conditions.append(Task.status == status)
    if due_only:
        conditions.append(Task.due_date.isnot(None))
        conditions.append(Task.due_date <= datetime.utcnow())

    async with _session_factory()() as db:
        query = select(Task)
        if conditions:
            query = query.where(*conditions)
        rows = (
            await db.execute(
                query.order_by(Task.due_date.is_(None), Task.due_date, Task.id).limit(limit)
            )
        ).scalars().all()

        out = []
        for task in rows:
            company = None
            if task.customer_id:
                company = (
                    await db.execute(
                        select(Customer.company_name).where(Customer.id == task.customer_id)
                    )
                ).scalar_one_or_none()
            out.append({
                "task_id": task.id,
                "title": task.title,
                "status": task.status if isinstance(task.status, str) else task.status.value,
                "priority": task.priority if isinstance(task.priority, str) else task.priority.value,
                "task_type": task.task_type,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "customer_id": task.customer_id,
                "company_name": company,
            })

    return {
        "count": len(out),
        "tasks": out,
        "filtered_by": {
            "customer_id": customer_id or None,
            "status": status or None,
            "due_only": due_only,
        },
    }


# ------------------------------------------------- templates, read back ---
# A template the agent can write but never read is a template it can never
# use: "send the quotation template to Hans" needs the body before it needs
# anything else. These two close that loop, and neither one writes.

@tool(
    description=(
        "List email templates that already exist, from the 邮件模板 (Templates) "
        "page — name, category, language, subject, and which placeholders each "
        "one uses. Filter by category or language, or search names and subjects. "
        "Read-only: check here before writing a new template, and use "
        "render_email_template to fill one in."
    ),
    tags={"read", "email"},
)
async def list_email_templates(
    category: str = "",
    language: str = "",
    search: str = "",
    limit: int = 10,
) -> dict:
    """See what is already on the shelf.

    ``create_email_template`` refuses to rewrite a name that is taken, so
    without a way to look, a second goal about the same template always ends
    in ``duplicate`` and the model never learns what the first one said.
    """
    limit = max(1, min(int(limit or 10), MAX_ROWS))
    category = (category or "").strip().lower()
    language = (language or "").strip().lower()

    if category and category not in TEMPLATE_CATEGORIES:
        return {
            "error": (
                f"unknown category {category!r}. The Templates page filters on "
                f"exactly these: {', '.join(TEMPLATE_CATEGORIES)}"
            )
        }
    if language and language not in TEMPLATE_LANGUAGES:
        return {
            "error": (
                f"unknown language {language!r}. Supported: "
                f"{', '.join(TEMPLATE_LANGUAGES)}"
            )
        }

    stmt = select(EmailTemplate)
    if category:
        stmt = stmt.where(EmailTemplate.category == category)
    if language:
        stmt = stmt.where(EmailTemplate.language == language)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(EmailTemplate.name.ilike(pattern), EmailTemplate.subject.ilike(pattern))
        )
    stmt = stmt.order_by(EmailTemplate.use_count.desc(), EmailTemplate.id.desc()).limit(limit)

    async with _session_factory()() as db:
        rows = (await db.execute(stmt)).scalars().all()

    return {
        "count": len(rows),
        "filters": {
            "category": category or None,
            "language": language or None,
            "search": search or None,
        },
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "language": t.language,
                "subject": t.subject,
                "body_preview": (t.body or "")[:400],
                "placeholders_used": _placeholders(f"{t.subject}\n{t.body or ''}"),
                "use_count": t.use_count or 0,
                "is_ai_generated": bool(t.is_ai_generated),
            }
            for t in rows
        ],
        "note": "read-only. Use render_email_template to fill one in for a customer.",
    }


@tool(
    description=(
        "Fill a saved template's placeholders and return the subject and body "
        "ready to pass to create_email_draft. Read-only — nothing is stored and "
        "nothing is sent. Pass customer_id to fill company, country, industry, "
        "product and the primary contact's name and title from the CRM, or set "
        "any of them by hand. A placeholder with no value is left standing and "
        "listed in unresolved_placeholders, so a typo stays visible instead of "
        "reaching a customer."
    ),
    tags={"read", "email"},
)
async def render_email_template(
    template_id: int,
    customer_id: int = 0,
    company_name: str = "",
    contact_name: str = "",
    contact_title: str = "",
    product_name: str = "",
    country: str = "",
    industry: str = "",
) -> dict:
    """Preview what a template would actually say.

    The fill goes through the same ``render_template`` the send path uses, so
    what comes back here is what a recipient would get — including the two
    brace dialects the editor accepts. Substituting by hand in the reply is
    how ``{comapny_name}`` ends up in a customer's inbox.
    """
    from app.utils.email_sender import render_template

    async with _session_factory()() as db:
        template = (
            await db.execute(select(EmailTemplate).where(EmailTemplate.id == int(template_id)))
        ).scalar_one_or_none()
        if template is None:
            return {"error": f"no template with id {template_id}"}

        customer = None
        contact = None
        if customer_id:
            customer = (
                await db.execute(select(Customer).where(Customer.id == int(customer_id)))
            ).scalar_one_or_none()
            if customer is None:
                return {"error": f"no customer with id {customer_id}"}
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

    # CRM values first, explicit arguments second: a hand-set value wins over
    # what the record says, because the person asking is the one who knows
    # what they meant.
    variables = {
        "company_name": (customer.company_name if customer else "") or "",
        "country": (customer.country if customer else "") or "",
        "industry": (customer.industry if customer else "") or "",
        "product_name": (customer.products if customer else "") or "",
        "contact_name": (contact.name if contact else "") or "",
        "contact_title": (contact.title if contact else "") or "",
    }
    for key, value in (
        ("company_name", company_name),
        ("contact_name", contact_name),
        ("contact_title", contact_title),
        ("product_name", product_name),
        ("country", country),
        ("industry", industry),
    ):
        if (value or "").strip():
            variables[key] = value.strip()

    # Only the filled ones are handed to the renderer. ``render_template``
    # substitutes every name it is given and blanks the rest, so passing an
    # empty ``contact_name`` would silently eat the placeholder instead of
    # leaving ``{contact_name}`` standing where a reader can see it.
    filled = {k: v for k, v in variables.items() if (v or "").strip()}

    subject = render_template(template.subject, filled)
    body = render_template(template.body, filled)
    unresolved = _unresolved(f"{subject}\n{body}")

    return {
        "template_id": template.id,
        "template_name": template.name,
        "customer_id": customer.id if customer else None,
        "variables_used": filled,
        "subject": subject,
        "body": body,
        "unresolved_placeholders": unresolved or None,
        "next_step": (
            "pass subject and body to create_email_draft — it stores the draft "
            "for human review and sends nothing"
        ),
    }


# ------------------------------------------------------- the mail queue ---
#: The statuses the Emails page filters on. A status outside this set is not
#: a spelling problem — it is a filter that would silently return nothing.
EMAIL_STATUSES = (
    "pending", "sending", "sent", "delivered", "opened",
    "replied", "rejected", "bounced", "failed",
)


@tool(
    description=(
        "List email logs across the whole CRM — the queue behind the 邮件 "
        "(Emails) page. status='pending' is what is waiting for a human to "
        "review and send; 'rejected' is what a person turned down; 'replied' "
        "is what came back. Filter by status or by one customer. Read-only."
    ),
    tags={"read", "email"},
)
async def list_email_logs(
    status: str = "",
    customer_id: int = 0,
    limit: int = 15,
) -> dict:
    """The queue, seen whole.

    ``get_communication_history`` answers "what has this customer received";
    this answers "what is on the desk". Without it the agent writes a second
    draft for a customer whose first is still waiting, because its only
    duplicate check is same-subject-same-customer — it cannot see that the
    review queue is already three deep.
    """
    limit = max(1, min(int(limit or 15), MAX_ROWS))
    status = (status or "").strip().lower()
    if status and status not in EMAIL_STATUSES:
        return {"error": f"unknown status {status!r}. One of: {', '.join(EMAIL_STATUSES)}"}

    stmt = select(EmailLog)
    if status:
        stmt = stmt.where(EmailLog.status == status)
    if customer_id:
        stmt = stmt.where(EmailLog.customer_id == int(customer_id))
    stmt = stmt.order_by(EmailLog.created_at.desc(), EmailLog.id.desc()).limit(limit)

    async with _session_factory()() as db:
        rows = (await db.execute(stmt)).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        key = _status(row.status)
        counts[key] = counts.get(key, 0) + 1

    return {
        "count": len(rows),
        "filters": {"status": status or None, "customer_id": customer_id or None},
        "counts_by_status": counts,
        "emails": [
            {
                "id": e.id,
                "customer_id": e.customer_id,
                "to_email": e.to_email,
                "to_name": e.to_name,
                "subject": e.subject,
                "status": _status(e.status),
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "sent_at": e.sent_at.isoformat() if e.sent_at else None,
                "body_preview": (e.body or "")[:400],
            }
            for e in rows
        ],
    }


# ----------------------------------------------------- business overview ---

@tool(
    description=(
        "The 仪表盘 (Dashboard) numbers: how many customers sit at each "
        "pipeline stage, open and reply rates, and the task backlog. Read-only "
        "facts with no verdict attached — you decide what they mean."
    ),
    tags={"read", "crm"},
)
async def get_pipeline_stats() -> dict:
    """The book of business in aggregate.

    ``list_customers`` can answer the same question one page at a time, but a
    funnel question is a count question: asked of a capped list it comes back
    wrong whenever the pipeline is longer than the cap, and wrong confidently.
    """
    from app.services.analytics_service import get_dashboard_stats, get_pipeline_funnel

    async with _session_factory()() as db:
        stats = await get_dashboard_stats(db)
        funnel = await get_pipeline_funnel(db)

    return {
        "dashboard": stats,
        "stages": {entry["stage"].lower(): entry["count"] for entry in funnel["stages"]},
        "total_customers_in_funnel": funnel["total"],
    }


@tool(
    description=(
        "Operational quality of the agent itself over the last N days: runs by "
        "status and stop reason, latency, tool calls, and the email funnel. "
        "Read-only. Use it to answer how the system has been behaving — it says "
        "nothing about any customer."
    ),
    tags={"read", "ops"},
)
async def get_quality_report(days: int = 7) -> dict:
    """The system's own record, for when the question is about the system."""
    from app.services.quality_service import get_quality_report

    days = max(1, min(int(days or 7), 90))
    async with _session_factory()() as db:
        report = await get_quality_report(db, days=days)
    report = dict(report)
    report["days"] = days
    return report


# --------------------------------------------- stage, date, and withdrawal ---
# The rest of the state a person moves by hand: where a customer stands in the
# pipeline, when a task is due, and whether a pending draft is still wanted.
# Each one moves exactly one field and edits nothing anybody wrote.

#: The pipeline, in the order a relationship moves through it. The order is
#: not enforced — a customer can jump from new to lost — but the vocabulary
#: is: a stage the Customers page cannot display is a stage nobody can find.
CUSTOMER_STAGES = (
    "new", "contacted", "interested", "quoting",
    "sample", "ordering", "completed", "lost",
)


@tool(
    description=(
        "Move a customer to a different pipeline stage: new, contacted, "
        "interested, quoting, sample, ordering, completed or lost. Only the "
        "stage changes — the company's details, contacts and notes are left "
        "exactly as they are. Use it when you tell me where a customer now "
        "stands. dry_run=true previews without writing."
    ),
    tags={"write", "crm"},
)
async def update_customer_stage(
    customer_id: int,
    stage: str,
    dry_run: bool = False,
) -> dict:
    """Record where a relationship now stands.

    ``upsert_customer`` refuses to touch an existing row because an agent
    silently rewriting curated data is worse than a duplicate. A stage is not
    curated data — it is the one field whose whole purpose is to change, and
    ``complete_task`` set the precedent: flipping a status is bookkeeping
    anybody can check. Everything else on the row is off limits, which is why
    this tool takes no other parameter.
    """
    stage = (stage or "").strip().lower()
    if stage not in CUSTOMER_STAGES:
        return {"error": f"unknown stage {stage!r}. One of: {', '.join(CUSTOMER_STAGES)}"}

    async with _session_factory()() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.id == int(customer_id)))
        ).scalar_one_or_none()
        if customer is None:
            return {"error": f"no customer with id {customer_id}"}

        current = _status(customer.stage)
        if current == stage:
            return {
                "status": "unchanged",
                "customer_id": customer.id,
                "company_name": customer.company_name,
                "stage": current,
                "reason": "the customer is already in this stage. Nothing was changed.",
            }

        if dry_run:
            return {
                "status": "would_change",
                "dry_run": True,
                "customer_id": customer.id,
                "company_name": customer.company_name,
                "stage_before": current,
                "stage_after": stage,
            }

        customer.stage = CustomerStage[stage.upper()]
        await db.commit()
        await db.refresh(customer)
        return {
            "status": "stage_updated",
            "customer_id": customer.id,
            "company_name": customer.company_name,
            "stage_before": current,
            "stage_after": _status(customer.stage),
            "visible_in": "客户 (Customers) page and the customer's detail page",
        }


@tool(
    description=(
        "Move a task's due date, or clear it by passing an empty string. Only "
        "the due date changes — the title, description, priority and status "
        "stay exactly as they are. Write the date as YYYY-MM-DD. A task that is "
        "already closed is not rescheduled. dry_run=true previews without "
        "writing."
    ),
    tags={"write", "tasks"},
)
async def reschedule_task(
    task_id: int,
    due_date: str,
    dry_run: bool = False,
) -> dict:
    """Move the date, and only the date.

    ``complete_task`` closes a task without touching its words; this moves its
    date without touching its words. Rewriting a title or a description is
    editing somebody's plan — deciding when the plan happens next is the part
    a person hands over every day ("push that to next week"), so it is the one
    field here that can move.
    """
    due, due_error = _parse_due(due_date)
    if due_error:
        return {"error": due_error}

    async with _session_factory()() as db:
        task = (
            await db.execute(select(Task).where(Task.id == int(task_id)))
        ).scalar_one_or_none()
        if task is None:
            return {"error": f"no task with id {task_id}"}

        current_status = task.status if isinstance(task.status, str) else task.status.value
        if current_status in ("completed", "cancelled"):
            return {
                "status": "already_closed",
                "task_id": task.id,
                "title": task.title,
                "current_status": current_status,
                "reason": (
                    "this task is already closed, so there is nothing left to "
                    "schedule. Create a new task if the work is back on."
                ),
            }

        before = task.due_date.isoformat() if task.due_date else None
        after = due.isoformat() if due else None
        if before == after:
            return {
                "status": "unchanged",
                "task_id": task.id,
                "title": task.title,
                "due_date": before,
                "reason": "the task is already due then. Nothing was changed.",
            }

        if dry_run:
            return {
                "status": "would_reschedule",
                "dry_run": True,
                "task_id": task.id,
                "title": task.title,
                "due_date_before": before,
                "due_date_after": after,
            }

        task.due_date = due
        await db.commit()
        await db.refresh(task)
        return {
            "status": "rescheduled",
            "task_id": task.id,
            "title": task.title,
            "due_date_before": before,
            "due_date_after": task.due_date.isoformat() if task.due_date else None,
            "visible_in": "待办事项 (Tasks) page",
        }


@tool(
    description=(
        "Withdraw an email draft that is still waiting for review: it is "
        "marked rejected with your reason and KEPT as a record — never "
        "deleted, and never sent. Only a draft in 'pending' can be rejected; "
        "anything already sent is left alone."
    ),
    tags={"write", "email"},
)
async def reject_email_draft(
    email_id: int,
    reason: str = "",
    dry_run: bool = False,
) -> dict:
    """Turn down a proposal, keeping the evidence.

    The API's own reject endpoint keeps the row for the same reason this tool
    does: a draft a person turned down is information the next run needs
    ("that approach was already refused"), and a deletion carries none of it.
    A status flip, like ``complete_task`` — no content is edited.
    """
    reason = (reason or "").strip()

    async with _session_factory()() as db:
        draft = (
            await db.execute(select(EmailLog).where(EmailLog.id == int(email_id)))
        ).scalar_one_or_none()
        if draft is None:
            return {"error": f"no email log with id {email_id}"}

        current = _status(draft.status)
        if current == "rejected":
            return {
                "status": "already_rejected",
                "email_log_id": draft.id,
                "customer_id": draft.customer_id,
                "subject": draft.subject,
                "reason_recorded": draft.error_message,
            }
        if current != "pending":
            return {
                "error": (
                    f"only a draft waiting for review can be rejected; this one "
                    f"is '{current}'. Nothing was changed."
                )
            }

        if dry_run:
            return {
                "status": "would_reject",
                "dry_run": True,
                "email_log_id": draft.id,
                "customer_id": draft.customer_id,
                "subject": draft.subject,
            }

        draft.status = EmailStatus.REJECTED
        draft.error_message = reason or "rejected by the agent"
        await db.commit()
        await db.refresh(draft)
        return {
            "status": "rejected",
            "email_log_id": draft.id,
            "customer_id": draft.customer_id,
            "subject": draft.subject,
            "reason_recorded": draft.error_message,
            "note": "the draft is kept as a record. It was not sent and not deleted.",
        }


# ------------------------------------------------------- notes, across threads ---
# Three tools that make the memory real: write a note, drop one, read them back.
# They touch ``agent_memories`` only -- a table the agent owns, not the book.
#
# That is why ``forget`` is allowed to delete here while nothing in the business
# schema ever is. The boundary this codebase draws is about *a person's* data: a
# saved note, a sent email, a customer row. A working preference the model wrote
# down is the model's own bookkeeping, and a wrong one is worse than none -- it
# would silently steer every later thread. Deleting it has nothing to audit
# against, which is precisely why it is allowed.

@tool(
    description=(
        "Keep one durable note across conversations: a standing preference, a "
        "rule the operator stated, how they want work done. The note survives "
        "this thread and is shown to you in later ones. Use the same key to "
        "change it — writing 'preferred quote currency' twice replaces the first "
        "value instead of adding a second note. NOT for business facts: a "
        "customer's buyer, address or stage belongs in add_customer_note or "
        "update_customer_stage, where it sits on the customer and is visible on "
        "that customer's page."
    ),
    tags={"write", "memory"},
)
async def remember(key: str, value: str) -> dict:
    from app.agent.memory import remember as _remember

    return await _remember(key, value)


@tool(
    description=(
        "Drop one note that has stopped being true. There is no archive — the "
        "note is gone, and the operator can always state the preference again. "
        "Use list_memories first to get the exact key."
    ),
    tags={"write", "memory"},
)
async def forget(key: str) -> dict:
    from app.agent.memory import forget as _forget

    return await _forget(key)


@tool(
    description=(
        "Read back the notes kept across conversations. Check here before "
        "telling the operator you do not know how they want something done — it "
        "may already be written down. Read-only."
    ),
    tags={"read", "memory"},
)
async def list_memories() -> dict:
    from app.agent.memory import list_memories as _list_memories

    rows = await _list_memories()
    return {"count": len(rows), "memories": rows}


# Tools the agent must never have. Kept as an explicit list so the boundary is
# testable rather than merely absent: tests/test_agent_tools_policy.py asserts
# no registered tool name matches these.
FORBIDDEN_TOOL_PATTERNS = ("send", "dispatch", "delete", "remove", "drop", "purge")


def build_trade_registry() -> ToolRegistry:
    """Every tool the agent may call.

    Enforced here by omission rather than by asking the model to behave: there
    is no send tool, no delete tool, and no tool that rewrites the content a
    person saved. The tools that touch an existing row at all — closing a
    task, moving a stage, moving a due date, withdrawing a pending draft —
    each move exactly one status or schedule field and nothing else.
    """
    registry = ToolRegistry()
    for entry in (
        list_customers,
        get_customer,
        get_communication_history,
        get_customer_signals,
        get_customer_assessment,
        get_follow_up_plan,
        get_pipeline_stats,
        get_quality_report,
        search_prospects,
        inspect_website,
        classify_prospect,
        list_tasks,
        list_email_logs,
        list_email_templates,
        render_email_template,
        upsert_customer,
        add_customer_contact,
        add_customer_note,
        create_task,
        complete_task,
        reschedule_task,
        update_customer_stage,
        reject_email_draft,
        create_email_draft,
        create_email_template,
        remember,
        forget,
        list_memories,
    ):
        registry.register(entry)
    return registry
