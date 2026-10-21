"""The website fetch, tested without a website.

A customer's site has no test environment: the live site *is* production, and
the page that works today is redesigned tomorrow. So this file does not try to
test "the fetch works". It pins the two things that can be decided offline:

* what counts as a page (chrome stripped, title read, text capped), and
* what counts as **no** page (a status code, a transport failure) -- because the
  failure mode that mattered here was a 404 body being analysed as the company's
  homepage, which produces a confident answer about a parked domain.

The HTML below is hand-written to the shape real pages take (a header, a nav, a
footer, inline scripts). It is not a recording of one specific site; a fixture
recorded from one site proves that site, not the parser.
"""
from __future__ import annotations

import pytest
import requests

from app.services.agent_service import (
    PAGE_TEXT_LIMIT,
    analyze_customer_website,
    extract_page_text,
)


@pytest.fixture(autouse=True)
def _allow_the_fake_host(monkeypatch):
    """``acme.test`` exists only here; the guard's policy has its own tests."""
    from app import url_guard

    monkeypatch.setattr(url_guard, "ensure_public_http_url", lambda url: url)


class _Response:
    """The slice of ``requests.Response`` this code actually touches."""

    def __init__(self, text: str, status_code: int = 200, url: str = "https://acme.test"):
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Not Found for url: {self.url}",
                response=self,
            )


PAGE = """<!doctype html>
<html><head>
  <title>Acme Textiles - Home</title>
  <style>.hero { color: red }</style>
</head><body>
  <header><div class="menu">Menu</div></header>
  <nav>Home | About | Contact</nav>
  <main>
    <h1>Acme Textiles</h1>
    <p>We import wholesale fabric for European retailers.</p>
  </main>
  <script>var tracking = "do not read me";</script>
  <footer>&copy; 2026 Acme Textiles</footer>
</body></html>"""


def test_the_parse_keeps_the_content_and_drops_the_chrome():
    text, title = extract_page_text(PAGE)

    assert title == "Acme Textiles - Home"
    assert "We import wholesale fabric" in text
    # A nav bar is not a description of the business.
    for chrome in ("Menu", "Home | About", "do not read me", "2026 Acme Textiles"):
        assert chrome not in text


def test_the_text_handed_to_the_model_is_capped():
    text, _ = extract_page_text("<p>" + ("a" * (PAGE_TEXT_LIMIT * 2)) + "</p>")

    assert len(text) == PAGE_TEXT_LIMIT


def test_a_page_without_a_title_reports_an_empty_one():
    # ``soup.title.string`` is None for a title with child tags; ``None`` used to
    # leak into the prompt and into ``company_name``.
    assert extract_page_text("<p>hello</p>") == ("hello", "")
    assert extract_page_text("<title>Acme</title><p>hi</p>") == ("hi", "Acme")


def test_a_title_of_markup_never_leaks_none_or_the_wrapper():
    """What the two parsers disagree on, and what must hold either way.

    ``<title>`` is an RCDATA element, so a strict parser (lxml 5.x) treats the
    nested markup as literal text and returns "<b>Acme</b>", while lxml 4.x
    parses it as a tag and returns "Acme". Asserting one of them makes the
    suite green on whichever machine it was written on and red everywhere
    else -- which is how CI failed while local stayed green. Assert the part
    that is the actual contract: no None (the original bug) and no wrapper.
    """
    page_text, title = extract_page_text("<title><b>Acme</b></title><p>hi</p>")

    assert page_text == "hi"
    assert "Acme" in title
    assert "<title>" not in title


@pytest.mark.asyncio
async def test_a_404_body_is_not_analysed_as_the_company_site(monkeypatch):
    """A 404 has a body. Parsing it produces a confident answer about nothing."""
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _Response("<title>Not Found</title><p>Domain for sale</p>", 404),
    )

    result = await analyze_customer_website("https://acme.test/missing")

    assert result["score"] is None
    assert "404" in result["error"]
    assert "company_name" not in result


@pytest.mark.asyncio
async def test_a_transport_failure_scores_nothing(monkeypatch):
    def _boom(*a, **k):
        raise requests.ConnectionError("nodename nor servname provided")

    monkeypatch.setattr(requests, "get", _boom)

    result = await analyze_customer_website("https://acme.test")

    assert result["score"] is None
    assert "Failed to fetch website" in result["error"]


@pytest.mark.asyncio
async def test_a_200_page_still_reaches_the_analysis(monkeypatch):
    """The status guard must not turn a working fetch into an error."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(PAGE))

    result = await analyze_customer_website("https://acme.test", "yarn")

    assert result["company_name"] == "Acme Textiles"
    assert result["ai_powered"] is False
    assert result["presence_signals"]["mentions_wholesale"] is True
