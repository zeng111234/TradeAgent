"""``GET /usage`` — what the models have cost since this process started.

The ledger is in memory and per process, which is a limit worth stating rather
than hiding: two workers each report their own half, and a restart starts again
at zero. What it does answer, today and for free, is the question that had no
answer at all before: which feature spent the tokens, and how much of that input
the provider served from cache.

A run's own row answers the same question for one run (``prompt_tokens`` /
``cached_prompt_tokens`` / ``cost``); this endpoint answers it across every call
site, including the eight in ``app.services`` that store no run at all.
"""
from fastapi import APIRouter

from app.llm_usage import snapshot

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
async def api_usage():
    """Token spend by scope and model, plus totals that say if they are complete.

    ``cost`` is ``null`` when no price was declared for that model
    (``LLM_PRICES_JSON``). ``null`` is not zero: it means the money is unknown,
    and ``cost_complete`` is ``false`` for exactly that reason.
    """
    return snapshot()
