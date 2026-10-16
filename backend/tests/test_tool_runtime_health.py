"""The tools have to be able to run, not just to be written.

`inspect_website` -- the tool the whole prospect-reading story rests on -- was
dead twice over, and nothing in the suite noticed for the same reason both
times: every test that exercises the scan path **substitutes** this tool with a
canned response (``tests/scripted_llm.py::scan_stubs``), so its body never ran.

1. ``requests`` is declared in ``backend/requirements.txt`` and was not
   installed. The import failed before the fetch, so the tool returned a
   ``ModuleNotFoundError`` observation on every call.
2. ``_extract_emails`` and ``_extract_phones`` were **called but never
   defined**. With ``requests`` installed, the first successful page fetch
   raised ``NameError`` at the line that builds the result.

Neither is exotic. Both are invisible to a suite that stubs the function.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import pytest

from app.agent.trade_tools import (
    _extract_emails,
    _extract_phones,
    inspect_website,
    search_prospects,
)


# --------------------------------------------------------------------------
# The environment has to match requirements.txt
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module,why",
    [
        ("requests", "inspect_website fetches pages with it"),
        ("bs4", "inspect_website parses pages with it (beautifulsoup4)"),
        ("lxml", "the parser bs4 is asked to use"),
        ("ddgs", "search_prospects searches with it"),
    ],
)
def test_the_declared_network_dependencies_are_importable(module, why):
    """A missing dependency must fail here, not degrade into a bad observation.

    Without this, an incomplete environment turns a working-looking tool into
    one that always answers "could not fetch: ModuleNotFoundError", and the
    agent reports that as a failed tool call rather than as a broken install.
    """
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise AssertionError(
            f"{module} is declared in backend/requirements.txt but not installed "
            f"({exc}); {why}"
        ) from exc


# --------------------------------------------------------------------------
# The extractors that did not exist
# --------------------------------------------------------------------------

def test_emails_are_found_and_deduplicated():
    raw = (
        '<a href="mailto:sales@example-textil.de">Mail</a> '
        '<a href="mailto:SALES@example-textil.de">again</a> '
        "or write to hans.mueller@example-textil.de."
    )
    found = _extract_emails(raw)

    assert "sales@example-textil.de" in [f.lower() for f in found]
    assert len([f for f in found if f.lower() == "sales@example-textil.de"]) == 1
    assert "hans.mueller@example-textil.de" in found


def test_stylesheet_filenames_are_not_mistaken_for_addresses():
    """``sprite@2x.png`` matches an email pattern; the extension is the tell."""
    raw = '.icon{background:url("sprite@2x.png")} .bg{background:url(a@3x.webp)}'

    assert _extract_emails(raw) == []


def test_phones_are_found_in_the_shapes_trade_pages_use():
    text = (
        "Tel: +49 (0)40 123-4567  "
        "Mobile: 0049 172 9876543  "
        "Orders: 2024  "
        "Ref: 99887766554433221100"
    )
    found = _extract_phones(text)

    digits = ["".join(c for c in f if c.isdigit()) for f in found]
    assert any(d.endswith("1234567") for d in digits), found
    assert any("9876543" in d for d in digits), found
    assert not any(d == "2024" for d in digits), "a year is not a phone number"
    assert not any(len(d) > 15 for d in digits), "a long id is not a phone number"


# --------------------------------------------------------------------------
# The tool, end to end, without the network
# --------------------------------------------------------------------------

class FakeResponse:
    status_code = 200
    text = (
        "<html><head><title>Nordstickerei Handel GmbH</title></head>"
        "<body><p>Wholesale embroidery supplies.</p>"
        '<a href="mailto:einkauf@nordstickerei.de">Contact</a>'
        "<p>Tel: +49 40 1234567</p></body></html>"
    )


def test_inspect_website_returns_a_usable_page(monkeypatch):
    """The whole body, which no test ran before.

    This is the test that fails with ``NameError: name '_extract_emails' is not
    defined`` on the old code -- as long as ``requests`` is installed, which is
    the other half of the defect.
    """
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())

    result = asyncio.run(inspect_website(url="https://nordstickerei.example"))

    assert "error" not in result, result
    assert result["status_code"] == 200
    assert result["title"] == "Nordstickerei Handel GmbH"
    assert "einkauf@nordstickerei.de" in result["emails"]
    assert result["phones"], "the phone number on the page was not found"
    assert "embroidery" in result["text"]


def test_inspect_website_reports_a_failed_fetch_as_data(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(requests, "get", refuse)

    result = asyncio.run(inspect_website(url="https://nope.example"))

    assert "error" in result
    assert "OSError" in result["error"]


def test_search_prospects_is_reachable(monkeypatch):
    """Its module import was missing too, so a search could never run."""
    import ddgs

    class FakeDDGS:
        def text(self, query, max_results=8):
            return [{
                "title": "Example Textil GmbH - Importer",
                "href": "https://example-textil.example/products",
                "body": "Importer of embroidery threads",
            }]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)

    result = asyncio.run(search_prospects(keyword="embroidery thread", country="Germany"))

    assert result["errors"] == [], result["errors"]
    assert result["count"] == 1
    assert result["candidates"][0]["domain"] == "example-textil.example"
    assert result["candidates"][0]["company_name"] == "Example Textil GmbH"
