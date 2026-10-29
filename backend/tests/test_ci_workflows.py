"""The CI workflows are code, and two of their properties have already broken.

Both failures were invisible to the offline suite, because the suite never reads
``.github/``: the suite tests the app, and these are statements about the *job*
that installs and runs the app. Eight consecutive red Contract Canary runs and a
weekly smoke job that had never once asked the provider a question are enough
evidence that "somebody will notice" is not a mechanism.

Two invariants, each from a real incident:

1. A job that installs ``openai`` must install ``httpx`` too. openai 1.6.1 hands
   ``proxies=`` to the httpx client, and httpx 0.28 deleted that argument. The
   canary installed openai alone, so pip took the newest httpx and the job died
   while *constructing* a client -- before it asked anything. The error read
   ``TypeError: AsyncClient.__init__() got an unexpected keyword argument
   'proxies'``, which sounds like our code and was not.

2. A workflow's default provider must be the one ``app/config.py`` ships. The
   smoke job defaulted to deepseek + ``deepseek-chat`` while the config default
   (and the secret that was actually configured) were a different provider, so
   every run answered ``Unsupported model deepseek-chat``. A smoke test against a
   provider the app never uses is measuring the wrong thing even when it is green.

These assert the files agree with each other, not that the values are good. If
the provider changes, change ``app/config.py`` and the workflow together and the
test keeps passing -- which is the point.
"""
import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CONFIG = Path(__file__).resolve().parents[1] / "app" / "config.py"


def _workflow(name: str) -> str:
    path = WORKFLOWS / name
    if not path.exists():
        pytest.skip(f"{name} is not present in this checkout")
    return path.read_text(encoding="utf-8")


def test_every_job_that_installs_openai_also_pins_httpx():
    """The pair, or nothing: openai 1.6.1 cannot construct a client on httpx 0.28."""
    for name in ("contract-canary.yml", "real-smoke.yml"):
        text = _workflow(name)
        installs_openai = re.search(r"pip install[^\n]*\bopenai==", text)
        if not installs_openai:
            continue
        assert re.search(r"pip install[^\n]*\bhttpx==", text), (
            f"{name} installs a pinned openai without pinning httpx; pip will "
            "take the newest httpx and the job will fail in the transport layer "
            "before it asks the provider anything"
        )


def _config_default(field: str) -> str:
    text = CONFIG.read_text(encoding="utf-8")
    match = re.search(rf'{field}:\s*str\s*=\s*"([^"]+)"', text)
    assert match is not None, f"{field} has no string default in app/config.py"
    return match.group(1)


def _workflows_with_a_provider_default():
    """Every workflow that names a provider, not just the one that broke.

    The first version of this test hardcoded real-smoke.yml, and the other two
    scheduled jobs kept the stale xiaomimimo default for another week. A test
    that enumerates one filename is a test that the other files cannot fail.
    """
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "OPENAI_BASE_URL" in text or "OPENAI_MODEL" in text:
            found.append((path.name, text))
    assert found, "no workflow sets a provider; the invariant would be vacuous"
    return found


def test_every_workflow_defaults_to_the_provider_the_app_ships():
    """A default that disagrees with the app tests a provider the app never uses."""
    for name, text in _workflows_with_a_provider_default():
        for field in ("OPENAI_BASE_URL", "OPENAI_MODEL"):
            expected = _config_default(field)
            assert f"{field}: ${{{{ secrets.{field} || '{expected}' }}}}" in text, (
                f"{name} does not default {field} to {expected!r}, the value "
                "app/config.py ships; the job would exercise a different provider"
            )
