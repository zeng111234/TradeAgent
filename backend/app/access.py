"""A shared-password gate for a public deployment.

Why it exists
-------------
Published on one link, this app is reachable by anyone with that link, and it
can spend money: every agent run costs tokens, and a configured mailbox sends
mail from a real address. The gate is what keeps "shareable" from also meaning
"an open wallet". One password, one cookie -- enough for the people the link
was meant for, and unlike a per-person login it needs no user store to ship.

Why it is inert by default
--------------------------
``APP_PASSWORD`` empty means this module does nothing: no redirect, no cookie,
no 401, and no login route that means anything. A local instance keeps behaving
exactly as it did before this file existed, which is also what makes the gate
safe to leave in the tree.

What it is not
--------------
Not an authentication system: there are no accounts, no roles, and no audit of
who did what. The cookie proves somebody was told the password, and nothing
more. It is deliberately not persisted across restarts -- a redeploy signing
people out is better than a stolen cookie staying valid.

Brute force, and why it is the part that matters here
-----------------------------------------------------
A short numeric password is enumerable in seconds from the public internet, so
the throttle is not decoration: failures are counted per source and *globally*,
and a global burst pauses logins entirely rather than letting one address walk
the whole keyspace from many addresses at once.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import defaultdict
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "ta_session"

#: How long one successful sign-in lasts. Long enough that a person working
#: through the afternoon is not thrown out mid-sentence; short enough that a
#: browser left open on a shared machine is not a permanent key.
SESSION_TTL_SECONDS = 12 * 3600

#: Failures tolerated per source before that source is paused.
MAX_FAILURES_PER_SOURCE = 5
LOCKOUT_SECONDS = 15 * 60
#: Failures tolerated across *all* sources in one window. This is the one that
#: stops a distributed walk of a short keyspace, which per-source counting by
#: itself does not: a bot that spreads attempts over many addresses never trips
#: a per-address limit.
MAX_FAILURES_GLOBAL = 20
GLOBAL_WINDOW_SECONDS = 15 * 60

#: Paths that must answer without a session. The probes are what a platform
#: asks before it sends traffic, and the assets are what the login page itself
#: is made of -- gating them would make the gate unopenable.
EXEMPT_PREFIXES = ("/login", "/health", "/ready", "/assets/", "/favicon")

router = APIRouter(tags=["Access"])


# --------------------------------------------------------------------------
# The cookie
# --------------------------------------------------------------------------

def enabled() -> bool:
    """Whether the gate is armed. Empty password, no gate."""
    return bool(settings.APP_PASSWORD)


def _key() -> bytes:
    return hashlib.sha256(settings.APP_PASSWORD.encode("utf-8")).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(now: float | None = None) -> str:
    """A cookie value: an expiry timestamp, signed.

    The payload is not encrypted and does not need to be -- it carries one
    number. What makes it unforgeable is the signature, and what makes it
    expire is that the number is checked on every request.
    """
    expires = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    payload = str(expires)
    return f"{payload}.{_sign(payload)}"


def session_is_valid(value: str | None, now: float | None = None) -> bool:
    """True for a cookie this process signed and that has not expired.

    Compared in constant time: a comparison that returns early on the first
    differing byte lets an attacker hear how much of a guess was right.
    """
    if not value or "." not in value:
        return False
    payload, _, signature = value.rpartition(".")
    if not payload.isdigit():
        return False
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    return int(payload) > (now if now is not None else time.time())


# --------------------------------------------------------------------------
# Failure counting
# --------------------------------------------------------------------------

class _Throttle:
    """In-memory failure counts. Nothing here survives a restart.

    That is the right trade: a throttle in the database would need a schema,
    and one in a cache would need a cache -- neither of which this app has.
    A restart clearing the counts costs an attacker only the time they had
    already spent.
    """

    def __init__(self) -> None:
        self.per_source: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
        self.global_failures: list[float] = []

    def locked(self, source: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        count, last = self.per_source.get(source, (0, 0.0))
        if count >= MAX_FAILURES_PER_SOURCE and now - last < LOCKOUT_SECONDS:
            return True
        self.global_failures = [t for t in self.global_failures if now - t < GLOBAL_WINDOW_SECONDS]
        return len(self.global_failures) >= MAX_FAILURES_GLOBAL

    def record_failure(self, source: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        count, _ = self.per_source.get(source, (0, 0.0))
        self.per_source[source] = (count + 1, now)
        self.global_failures.append(now)
        logger.warning("failed sign-in from %s (%d from this source)", source, count + 1)

    def reset(self, source: str) -> None:
        self.per_source.pop(source, None)


throttle = _Throttle()


def _source(request: Request) -> str:
    """Who to count a failure against.

    Behind a reverse proxy the peer address is the proxy's, which would make
    every attempt come from one source and turn the global counter into the
    only working limit -- so the forwarded header is honoured when present.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# The login page
# --------------------------------------------------------------------------

# The gate's own copy. It cannot come from the Vue app's i18n bundle: this page
# has to render when that bundle is unreachable (see the note below), so the two
# languages live here as plain strings.
_LOGIN_COPY = {
    "zh": {
        "html_lang": "zh-CN",
        "title": "TradeAgent — 访问密码",
        "tagline": "外贸业务员的 AI 助手：找客户、写开发信、跟进订单。",
        "gate": "这是一个私有部署，需要访问密码。",
        "ask": "密码在把这条链接发给你的人手里。",
        "placeholder": "访问密码",
        "cta": "进入",
    },
    "en": {
        "html_lang": "en",
        "title": "TradeAgent — Access password",
        "tagline": "The AI assistant for export sales: find buyers, draft outreach, track follow-ups.",
        "gate": "This is a private deployment, so it asks for a password.",
        "ask": "The password comes from whoever shared this link with you.",
        "placeholder": "Access password",
        "cta": "Continue",
    },
}

# Error *codes* travel between the handlers and the page; the page turns them
# into a sentence in whichever language the visitor is reading. Passing the
# finished sentence instead is how the English text ended up hardcoded.
_LOGIN_ERRORS = {
    "wrong_password": {
        "zh": "密码不正确。",
        "en": "Wrong password.",
    },
    "throttled": {
        "zh": "尝试次数过多，请稍后再试。",
        "en": "Too many failed attempts. Try again later.",
    },
}


def _pick_lang(request: Request, explicit: str = "") -> str:
    """Chinese unless the browser asks for English, or the link forces one.

    The product's own default is Chinese, so anything we do not recognise --
    ``*``, ``de``, an empty header -- lands on Chinese rather than English.
    """
    if explicit in _LOGIN_COPY:
        return explicit
    first = (request.headers.get("accept-language") or "").split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "zh"


def login_page(next_path: str = "/", error: str = "", lang: str = "zh") -> str:
    """A standalone page, deliberately not part of the Vue app.

    The gate has to work when the app's own bundle cannot be reached, and a
    login form that depends on the SPA it is guarding is a deadlock. Plain
    HTML with inline styles: no asset to fail to load, and no change to any
    existing page.

    ``lang`` is a plain argument rather than something read from the request
    here, so the caller decides -- and so this stays a pure function of its
    arguments.
    """
    if lang not in _LOGIN_COPY:
        lang = "zh"
    copy = _LOGIN_COPY[lang]
    target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    if error in _LOGIN_ERRORS:
        note = f'<p class="err">{_LOGIN_ERRORS[error][lang]}</p>'
    elif error:
        # An unmapped string is shown rather than swallowed: a future caller
        # that invents a message should still see it.
        note = f'<p class="err">{escape(error)}</p>'
    else:
        note = ""
    return f"""<!doctype html>
<html lang="{copy['html_lang']}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{copy['title']}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    padding:24px; box-sizing:border-box;
    background:#141414; color:#e5eaf3;
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; }}
  .card {{ width:100%; max-width:380px; padding:28px; border-radius:12px;
    background:#1d1e1f; border:1px solid #303133; box-shadow:0 8px 28px rgba(0,0,0,.45); }}
  .brand {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; }}
  .mark {{ display:inline-flex; width:26px; height:26px; align-items:center; justify-content:center;
    border-radius:7px; background:linear-gradient(135deg,#409eff,#7a5cff); color:#fff; }}
  .langs {{ font-size:12px; color:#4c4d4f; }}
  .langs a {{ color:#8d9095; text-decoration:none; padding:2px 5px; }}
  .langs a.active {{ color:#409eff; }}
  h1 {{ margin:0 0 6px; font-size:19px; font-weight:600; letter-spacing:.3px; }}
  .sub {{ margin:0 0 16px; color:#c8ccd4; font-size:13px; }}
  .gate {{ margin:0 0 12px; color:#8d9095; font-size:12px; }}
  .ask {{ margin:16px 0 0; color:#8d9095; font-size:12px; }}
  input {{ width:100%; box-sizing:border-box; padding:10px 12px; margin-bottom:14px;
    border-radius:6px; border:1px solid #4c4d4f; background:#262727; color:#e5eaf3; font-size:14px; }}
  input:focus {{ outline:none; border-color:#409eff; }}
  input[type=hidden] {{ display:none; }}
  button {{ width:100%; padding:10px; border:0; border-radius:6px; background:#409eff;
    color:#fff; font-size:14px; font-weight:600; cursor:pointer; }}
  button:hover {{ background:#66b1ff; }}
  .err {{ color:#f56c6c; font-size:12px; margin:10px 0 0; }}
</style></head>
<body><form class="card" method="post" action="/login">
  <div class="brand">
    <span class="mark" aria-hidden="true"><svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.9 5.6 5.6 1.9-5.6 1.9L12 17l-1.9-5.6L4.5 9.5l5.6-1.9L12 2zm6 12l.9 2.6 2.6.9-2.6.9-.9 2.6-.9-2.6-2.6-.9 2.6-.9.9-2.6z"/></svg></span>
    <span class="langs">
      <a href="/login?lang=zh&amp;next={quote(target)}" class="{'active' if lang == 'zh' else ''}">中文</a>·
      <a href="/login?lang=en&amp;next={quote(target)}" class="{'active' if lang == 'en' else ''}">EN</a>
    </span>
  </div>
  <h1>TradeAgent</h1>
  <p class="sub">{copy['tagline']}</p>
  <p class="gate">{copy['gate']}</p>
  {note}
  <input type="password" name="password" placeholder="{copy['placeholder']}" autocomplete="current-password" autofocus required>
  <input type="hidden" name="next" value="{quote(target)}">
  <input type="hidden" name="lang" value="{lang}">
  <button type="submit">{copy['cta']}</button>
  <p class="ask">{copy['ask']}</p>
</form></body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    """The gate's own page. Always answers, armed or not.

    ``lang`` is read out of the query string instead of being declared as a
    parameter on purpose: the typed HTTP surface for this route is frozen in
    ``tests/api_surface.json``, and a declared parameter would add to it. The
    page still honours ``?lang=zh`` / ``?lang=en``.
    """
    lang = _pick_lang(request, request.query_params.get("lang", ""))
    return HTMLResponse(login_page(next, lang=lang))


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(default=""),
    next: str = Form(default="/"),
    lang: str = Form(default=""),
):
    """Check the password and hand back a cookie.

    With no password configured there is nothing to check and nothing to hand
    out: the response says so rather than sending the caller somewhere they did
    not ask to go.
    """
    if not enabled():
        return JSONResponse({"detail": "the access gate is not enabled"}, status_code=404)

    source = _source(request)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    language = _pick_lang(request, lang)

    if throttle.locked(source):
        logger.warning("sign-in refused for %s: too many failures", source)
        return HTMLResponse(login_page(target, "throttled", language), status_code=429)

    if not password or not hmac.compare_digest(password, settings.APP_PASSWORD):
        throttle.record_failure(source)
        return HTMLResponse(login_page(target, "wrong_password", language), status_code=401)

    throttle.reset(source)
    response = RedirectResponse(target, status_code=303)
    # Secure only over https: a Secure cookie set on plain http is dropped by
    # the browser, which would silently break local testing of the gate.
    response.set_cookie(
        COOKIE_NAME,
        issue_session(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/logout")
async def logout():
    """Drop the cookie. There is no session to invalidate server-side."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# --------------------------------------------------------------------------
# The middleware
# --------------------------------------------------------------------------

class AccessGate(BaseHTTPMiddleware):
    """Require the cookie everywhere the login page does not cover."""

    async def dispatch(self, request: Request, call_next):
        if not enabled():
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        if session_is_valid(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)

        # An API caller gets a code it can branch on, not a login page:
        # a 200 text/html where JSON was expected is how a page ends up
        # rendering nothing and saying nothing about why.
        if path.startswith("/api/") or path in ("/docs", "/redoc") or path.startswith("/openapi"):
            return JSONResponse(
                {"detail": "not signed in", "code": "unauthenticated"},
                status_code=401,
            )
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
