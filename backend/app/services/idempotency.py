"""Retry-safe creates: the same request twice produces one row.

Only two operations in this codebase have external, irreversible effects -- an
email send and an agent run -- and both already had an idempotency mechanism.
The create endpoints did not, because a duplicate customer "can be merged". That
is true and it is still the wrong default: a client that times out after the
server committed has no way to know whether the row exists, and a retry is the
only recovery it has. Asking it to search first is asking it to reimplement the
server's transaction.

The design
----------
* A key is optional. Without one, behaviour is exactly what it was.
* With one, ``(scope, key)`` is claimed in the **same transaction** as the row.
  The unique index is the guarantee; the pre-check is only a fast path.
* ``request_hash`` separates a retry from a reused key: same key and same body
  replays; same key and different body is 409.
* A replay returns the original row *by id*, so it is serialised in today's
  shape. Storing a response body would replay a schema from last month.
* A row that has since been deleted is a 409, not a second create: the caller
  is asking about a request whose result no longer exists.

Races
-----
Two requests with the same key can both pass the pre-check. One insert wins;
the loser's flush raises ``IntegrityError``. The loser rolls back, re-reads the
winner's key, and replays. That is the same pattern as ``POST /agent/run``, and
it is why the unique index exists rather than a "check then insert" in Python.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError, ErrorCode
from app.models.idempotency_key import IdempotencyKey

#: A header value, not a payload. Long enough for a UUID with a scope prefix,
#: short enough that a stray request body cannot become a key.
MAX_KEY_LENGTH = 120


def clean_key(raw: Optional[str]) -> Optional[str]:
    """Validate the header, or return ``None`` when it was not sent.

    An empty header is refused rather than treated as absent: a client that
    sends ``Idempotency-Key:`` believes it is protected, and silently creating
    without the guarantee is worse than a 422.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            detail=(
                "Idempotency-Key was sent but empty. Omit the header if this "
                "request does not need to be retry-safe."
            ),
        )
    if len(key) > MAX_KEY_LENGTH:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            detail=(
                f"Idempotency-Key is {len(key)} characters; the limit is "
                f"{MAX_KEY_LENGTH}."
            ),
        )
    return key


def fingerprint(payload: Any) -> str:
    """A stable hash of the request body.

    Sorted keys and no whitespace: two JSON documents that mean the same thing
    must hash the same, or a harmless reordering would look like a different
    request. ``default=str`` covers dates and enums, which ``model_dump`` may
    leave in Python form.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_once(
    db: AsyncSession,
    *,
    scope: str,
    key: Optional[str],
    payload: Any,
    model: type,
    create: Callable[[], Awaitable[Any]],
    load: Optional[Callable[[Any], Awaitable[Any]]] = None,
) -> tuple[Any, bool]:
    """Create at most once for ``(scope, key)``.

    Returns ``(row, replayed)``. ``replayed`` is True when the row came from an
    earlier request; the caller puts it in a response header so a client can
    tell "I created this" from "this already existed".

    ``load`` exists for rows whose response touches relationships (a customer
    serialises contacts and notes). ``db.get`` would hand back an object whose
    lazy loads run outside the async greenlet -- the ``MissingGreenlet`` 500
    that ``customer_service.create_customer`` already documents.
    """
    if key is None:
        return await create(), False

    digest = fingerprint(payload)
    existing = await _find(db, scope, key)
    if existing is not None:
        return await _replay(db, existing, digest, model, load), True

    row = await create()
    if row is None:
        # The resource the caller named does not exist (a contact for a missing
        # customer). Nothing was created, so nothing is recorded.
        return None, False

    db.add(IdempotencyKey(
        scope=scope,
        key=key,
        request_hash=digest,
        response_id=row.id,
    ))

    try:
        await db.flush()
    except IntegrityError:
        # Somebody else claimed the key between the read and the insert. The
        # whole transaction is rolled back, including the business row just
        # created -- which is the point: only the winner's row may exist.
        await db.rollback()
        existing = await _find(db, scope, key)
        if existing is None:
            raise
        return await _replay(db, existing, digest, model, load), True

    return row, False


async def _find(db: AsyncSession, scope: str, key: str) -> Optional[IdempotencyKey]:
    result = await db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
    )
    return result.scalar_one_or_none()


async def _replay(db: AsyncSession, existing: IdempotencyKey, digest: str,
                  model: type, load: Optional[Callable[[Any], Awaitable[Any]]] = None) -> Any:
    if existing.request_hash != digest:
        raise ApiError(
            status_code=409,
            code=ErrorCode.IDEMPOTENCY_KEY_REUSE,
            detail=(
                "this Idempotency-Key was already used for a different request "
                "body; use a new key for a new request"
            ),
        )
    if load is not None:
        row = await load(existing.response_id)
    else:
        row = await db.get(model, existing.response_id)
    if row is None:
        raise ApiError(
            status_code=409,
            code=ErrorCode.IDEMPOTENCY_KEY_REUSE,
            detail=(
                "this Idempotency-Key produced a row that no longer exists; "
                "the request cannot be replayed"
            ),
        )
    return row
