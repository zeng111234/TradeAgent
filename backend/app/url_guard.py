"""Only public web pages may be fetched.

Two of this service's endpoints take a URL from outside: ``POST
/agent/analyze-website`` (a request body) and the agent's ``inspect_website``
tool (a URL the model chose, which can be influenced by page content). A fetch
of ``http://127.0.0.1:5432`` or ``http://169.254.169.254/latest/meta-data`` is
not a website, and on a host that is not a laptop it is a request into the
private network -- the SSRF shape.

What is enforced
----------------
* scheme is ``http``/``https`` (no ``file:``, ``ftp:``, ``gopher:``);
* no credentials embedded in the URL (``http://user:pass@host``);
* the host does not resolve to loopback, private, link-local, multicast,
  reserved or unspecified addresses, in IPv4 or IPv6, including IPv4-mapped
  IPv6 (``::ffff:127.0.0.1``);
* redirects are followed **manually**, one hop at a time, with the same check
  on every hop. ``requests``' own redirect handling would happily follow a
  public URL to ``http://127.0.0.1/``.

What is not enforced, and the reader should know
------------------------------------------------
DNS rebinding is still possible in principle: the address is checked, then
``requests`` resolves the name again to connect. Closing that requires pinning
the resolved address into the connection, which ``requests`` does not expose
without a custom adapter. For this service -- a single-process tool on a laptop
or a small VM -- the check removes the accidental and the casual case; it does
not claim to stop a determined attacker who controls DNS.

TLS verification is on by default (``WEBSITE_TLS_VERIFY``). It used to be
``verify=False`` at both fetch sites "because small trade sites have broken
chains", which meant every page read was also an unauthenticated page read.
A site with a broken chain is now reported as an error, which is the honest
outcome, and the setting exists for an operator who has decided otherwise.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 3

#: Redirect statuses worth following. 303 becomes a GET; the others preserve it.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class BlockedURL(ValueError):
    """The URL is not a public web page, so it was not fetched."""


def _address_problem(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[str]:
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_private:
        return "a private address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "a reserved address"
    if ip.is_unspecified:
        return "an unspecified address"
    if not ip.is_global:
        # Covers shared address space (100.64/10), documentation ranges and the
        # other blocks that are routable enough to look public but are not.
        return "not a globally routable address"
    return None


def _check_address(text: str, host: str) -> None:
    ip = ipaddress.ip_address(text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    problem = _address_problem(ip)
    if problem is not None:
        raise BlockedURL(
            f"{host!r} resolves to {ip}, {problem}; only public web pages may "
            "be fetched"
        )


def ensure_public_http_url(
    url: str,
    *,
    resolver: Optional[Callable[[str, Optional[int]], Iterable]] = None,
) -> str:
    """Return ``url`` unchanged, or raise :class:`BlockedURL`.

    ``resolver`` exists so tests can answer without DNS. It must return the
    shape ``socket.getaddrinfo`` returns; only the sockaddr's first element is
    read.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BlockedURL(f"{url!r} is not a valid URL: {exc}") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"{url!r} uses scheme {parts.scheme or '(none)'!r}; only http and "
            "https are fetched"
        )
    if parts.username or parts.password:
        raise BlockedURL(f"{url!r} embeds credentials; refusing to send them")

    host = parts.hostname
    if not host:
        raise BlockedURL(f"{url!r} has no host")

    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise BlockedURL(f"{host!r} is localhost; only public web pages may be fetched")

    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        resolve = resolver or socket.getaddrinfo
        try:
            infos = list(resolve(host, None))
        except OSError as exc:
            raise BlockedURL(f"could not resolve {host!r}: {exc}") from exc
        if not infos:
            raise BlockedURL(f"{host!r} did not resolve to an address")
        for info in infos:
            sockaddr = info[4]
            _check_address(sockaddr[0], host)
        return url

    _check_address(str(literal), host)
    return url


def fetch_public(
    url: str,
    *,
    timeout: float,
    headers: Optional[dict] = None,
    verify: bool = True,
    max_redirects: int = MAX_REDIRECTS,
):
    """``requests.get`` with a public-address check on every hop.

    Redirects are disabled on the underlying call and followed here, so the
    check cannot be bypassed by a ``Location`` header. Returns the final
    response, including non-2xx statuses -- deciding what a status means is the
    caller's job (``analyze_customer_website`` calls ``raise_for_status``; the
    agent tool classifies it).
    """
    import requests

    current = url
    for _ in range(max_redirects + 1):
        ensure_public_http_url(current)
        response = requests.get(
            current,
            timeout=timeout,
            headers=headers,
            verify=verify,
            allow_redirects=False,
        )
        location = (getattr(response, "headers", None) or {}).get("location")
        if response.status_code not in REDIRECT_STATUSES or not location:
            return response
        current = urljoin(current, location)

    raise BlockedURL(f"{url!r} redirected more than {max_redirects} times")
