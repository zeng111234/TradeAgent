"""The error contract, in one place.

A failure from this API has four parts, and they change at different rates:

    status      stable by RFC. Says who is wrong and whether to retry.
    code        stable for the life of ``/api/v1``. Says which condition.
    detail      free text. Says what a human should do. May be rewritten in
                any release; clients must not parse it.
    request_id  one per request. Also in the ``X-Request-Id`` response header
                and in the server log line. It is how "it failed at 14:02"
                becomes one specific request.

Two rules make ``code`` worth having:

1. **It is a category, not a sentence.** ``customer_not_found``, never
   ``customer_42_not_found``, never translated. An id inside a code means
   every id is a new code and a client cannot branch on a set that grows with
   the data. Ids and values go in ``detail`` (for humans) or ``details`` (for
   machines).

2. **A code never changes meaning.** A new condition gets a new code.
   Renaming or removing one is a breaking change for every client that
   branches on it, so it goes through the same gate as a field rename
   (``docs/问答记录.md`` R49: bump the path, keep v1 until the sunset date).

``retryable`` exists because the status code does not answer the question a
client actually has. 409 can mean "another send got there first, do not
bother" or "the row moved, re-read and retry"; 503 with ``Retry-After`` is a
retry and 503 without one is a guess. When the server knows, it says so.
Absent means *unknown*, and the client should fall back to the status code.

``retryable`` says whether a retry *can* succeed. It does not say a retry is
safe: re-sending a POST is safe only because the caller sends the same
idempotency key, not because the server called the first attempt retryable.

Why not put the code in a header (``X-Error-Code``)? Because a proxy that
drops unfamiliar headers turns a precise error into a vague one, and because
the body is what a client parses anyway. Headers are for things a proxy must
not drop (``Retry-After``, ``WWW-Authenticate``).
"""
from __future__ import annotations

import logging
import re
import uuid
from enum import Enum
from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

#: An inbound request id is echoed back so a caller can correlate its own logs.
#: It is *not* trusted: it goes into a log line and a response header, so a
#: value with newlines would forge log entries and a value with a colon could
#: split a header. Anything outside this alphabet is replaced, not sanitised --
#: a mangled id is worse than a fresh one because it looks correlated.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


class ErrorCode(str, Enum):
    """Every condition a caller may branch on.

    Grouped by the resource whose rule was broken, because that is how a
    caller finds the one it got: ``email_draft_claimed`` tells you where to
    look, ``conflict_3`` does not.

    The value is the wire format and is the contract. The member name is for
    Python only and may be renamed freely.
    """

    # -- generic. Emitted when a handler raised a bare HTTPException, so they
    #    are coarser than the specific codes below by construction.
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    UNAVAILABLE = "unavailable"

    # -- customers
    CUSTOMER_NOT_FOUND = "customer_not_found"
    CUSTOMER_HAS_MAIL = "customer_has_mail"
    CUSTOMER_MODIFIED = "customer_modified"

    # -- email logs and templates
    EMAIL_LOG_NOT_FOUND = "email_log_not_found"
    TEMPLATE_NOT_FOUND = "template_not_found"
    EMAIL_NOT_A_DRAFT = "email_not_a_draft"
    EMAIL_NOT_SENDABLE = "email_not_sendable"
    EMAIL_DRAFT_CLAIMED = "email_draft_claimed"
    EMAIL_SEND_IN_PROGRESS = "email_send_in_progress"
    EMAIL_SEND_FAILED = "email_send_failed"
    IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
    EMAIL_NEVER_DELIVERED = "email_never_delivered"
    REPLY_BEFORE_SENT = "reply_before_sent"
    REPLY_IN_FUTURE = "reply_in_future"
    SUBJECT_OR_BODY_REQUIRED = "subject_or_body_required"
    INVALID_LIMIT = "invalid_limit"

    # -- agent runs and one-off analyses
    RUN_NOT_FOUND = "run_not_found"
    LEADS_FILE_MISSING = "leads_file_missing"
    LLM_NOT_CONFIGURED = "llm_not_configured"
    AGENT_AT_CAPACITY = "agent_at_capacity"
    WAIT_BUDGET_TOO_LONG = "wait_budget_too_long"
    WEBSITE_UNREACHABLE = "website_unreachable"
    ANALYSIS_FAILED = "analysis_failed"
    DB_POOL_EXHAUSTED = "db_pool_exhausted"

    # -- tasks
    TASK_NOT_FOUND = "task_not_found"


#: Coarse codes for handlers that have not been migrated to ``ApiError`` yet,
#: plus the ones the framework raises on our behalf (404 on an unknown route,
#: 405 on the wrong method). They are deliberately generic: a caller that gets
#: ``not_found`` knows the request failed, not which resource was missing.
FALLBACK_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,  # no auth in v1; never emitted today
    403: ErrorCode.FORBIDDEN,  # no auth in v1; never emitted today
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.UNAVAILABLE,
    503: ErrorCode.UNAVAILABLE,
    504: ErrorCode.UNAVAILABLE,
}

#: Whether a second attempt *can* succeed, when the server knows.
#: ``None`` means unknown and is omitted from the body rather than guessed.
_RETRYABLE_BY_STATUS: dict[int, Optional[bool]] = {
    429: True,
    502: True,
    503: True,
    504: True,
}


def is_retryable(status_code: int) -> Optional[bool]:
    if status_code in _RETRYABLE_BY_STATUS:
        return True
    if 400 <= status_code < 500:
        return False
    # A 5xx that is not in the table is a bug, not a known transient. Saying
    # "do not retry" would be a guess in the other direction.
    return None


class ApiError(StarletteHTTPException):
    """An ``HTTPException`` that carries the contract.

    Subclasses Starlette's, not FastAPI's, so one handler catches it and the
    framework's own 404/405 take the same path.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        detail: str,
        *,
        retryable: Optional[bool] = None,
        details: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.retryable = retryable
        self.details = details


def request_id_of(request: Request) -> Optional[str]:
    # ``getattr(request, "state", None)`` and not ``request.state``: the pool
    # test calls the handler with a stand-in that has only ``method`` and
    # ``url``, and a handler that raises while formatting an error is the worst
    # possible place to be strict about the request object.
    return getattr(getattr(request, "state", None), "request_id", None)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    detail: Any,
    *,
    retryable: Optional[bool] = None,
    details: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        # ``detail`` is kept under its original key and original shape (a
        # string here, a list for validation errors). v1 already had it and
        # the frontend reads it; moving the message to ``error.message`` would
        # be a breaking change for a cosmetic gain (R49).
        "detail": detail,
        "code": code,
    }
    if retryable is not None:
        body["retryable"] = retryable
    if details:
        body["details"] = details
    request_id = request_id_of(request)
    if request_id:
        body["request_id"] = request_id
        # Also on the response, because the middleware that normally sets it
        # never sees a 500: an unhandled exception travels *through* the user
        # middleware stack to Starlette's error middleware, so the response is
        # built outside it. A caller looking for the id in one place should
        # find it in both.
        headers = dict(headers or {})
        headers.setdefault("X-Request-Id", request_id)

    # Every failure is logged with the id, at a level that matches who has to
    # act: a 4xx is the caller's problem (info), a 5xx is ours (error).
    log = logger.error if status_code >= 500 else logger.info
    log(
        "%s %s -> %s %s request_id=%s",
        request.method,
        request.url.path,
        status_code,
        code,
        request_id,
    )
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def http_error_response(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """One shape for every ``HTTPException``, declared or not."""
    code = getattr(exc, "code", None) or FALLBACK_CODES.get(
        exc.status_code, ErrorCode.INTERNAL_ERROR
    )
    code = code.value if isinstance(code, ErrorCode) else code
    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = is_retryable(exc.status_code)
    return error_response(
        request,
        exc.status_code,
        code,
        exc.detail,
        retryable=retryable,
        details=getattr(exc, "details", None),
        headers=getattr(exc, "headers", None),
    )


def validation_error_response(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A 422 that says which field, without echoing the body.

    Pydantic puts the offending input in every error (``input``) and, for
    some types, the exception in ``ctx``. FastAPI passes both through. That
    makes the response as large as the request -- a 20 000-character email
    comes back in full -- and puts customer text into browser consoles and
    error trackers. Neither is needed to fix the request: ``loc`` says which
    field and ``msg`` says what is wrong with it.

    ``detail`` stays a list, which is what v1 already returned for 422 and
    what a client that iterates it expects.
    """
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "type": error.get("type"),
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg"),
            }
        )
    return error_response(
        request,
        422,
        ErrorCode.VALIDATION_ERROR.value,
        errors,
        retryable=False,
    )


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def accept_request_id(raw: Optional[str]) -> str:
    """Echo a caller's id only if it is safe to put in a header and a log."""
    if raw and _REQUEST_ID.fullmatch(raw):
        return raw
    return new_request_id()
