"""A URL from a request body is not a licence to call the private network.

These tests are about refusal, not fetching: every blocked case asserts the
address never reached ``requests``. The redirect case is the one that would
have slipped through a check on the initial URL alone -- a public host can
answer ``302 Location: http://127.0.0.1/``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.url_guard import BlockedURL, ensure_public_http_url, fetch_public


def _resolver(mapping):
    def resolve(host, port):
        return [(2, 1, 6, "", (mapping[host], 0))]
    return resolve


@pytest.mark.parametrize("url", [
    "http://example.com/a",
    "https://example.com/a?b=1",
    "http://93.184.216.34/",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/",
])
def test_public_pages_pass(url):
    assert ensure_public_http_url(url) == url


@pytest.mark.parametrize("url", [
    "ftp://example.com/file",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http:///nohost",
    "http://user:secret@example.com/",
    "http://localhost/",
    "http://app.localhost/",
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://10.0.0.5/",
    "http://192.168.1.10/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data",
    "http://0.0.0.0/",
    "http://100.64.0.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[fe80::1]/",
])
def test_non_public_destinations_are_refused(url):
    with pytest.raises(BlockedURL):
        ensure_public_http_url(url)


def test_a_hostname_that_resolves_private_is_refused():
    with pytest.raises(BlockedURL) as caught:
        ensure_public_http_url(
            "http://internal.example/",
            resolver=_resolver({"internal.example": "10.1.2.3"}),
        )
    assert "private address" in str(caught.value)


def test_a_hostname_that_resolves_public_passes():
    url = "http://shop.example/"
    assert ensure_public_http_url(
        url, resolver=_resolver({"shop.example": "93.184.216.34"})
    ) == url


def test_a_name_that_does_not_resolve_is_refused_not_fetched():
    def broken(host, port):
        raise OSError("no such host")

    with pytest.raises(BlockedURL) as caught:
        ensure_public_http_url("http://nope.example/", resolver=broken)
    assert "could not resolve" in str(caught.value)


def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch):
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=302,
            headers={"location": "http://127.0.0.1:5432/"},
            text="",
        )

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(BlockedURL):
        fetch_public("https://93.184.216.34/", timeout=1.0)

    assert calls == ["https://93.184.216.34/"], (
        "the redirect target was requested; the guard only saw the first hop"
    )


def test_a_redirect_between_public_addresses_is_followed(monkeypatch):
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return SimpleNamespace(
                status_code=301,
                headers={"location": "https://93.184.216.35/x"},
                text="",
            )
        return SimpleNamespace(status_code=200, headers={}, text="ok")

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    assert fetch_public("https://93.184.216.34/", timeout=1.0).status_code == 200
    assert calls == [
        "https://93.184.216.34/",
        "https://93.184.216.35/x",
    ]


def test_the_website_analysis_endpoint_refuses_a_private_address():
    """The guard is wired into the request path, not just importable."""
    from app.services.agent_service import analyze_customer_website

    result = asyncio.run(analyze_customer_website("http://127.0.0.1:8000/admin"))
    assert "error" in result
    assert "only public web pages" in result["error"]
    assert result.get("score") is None
