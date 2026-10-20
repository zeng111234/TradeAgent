"""The PI number must identify an order for longer than one process lifetime.

A proforma invoice is a document the customer keeps, quotes back, and pays
against. If its number changes when the server restarts, the customer's copy
cannot be matched to ours -- which is exactly what ``hash()`` did: Python salts
string hashes per interpreter, so the same company produced 4511, 4227 and 0350
on three consecutive runs.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from app.services.agent_service import _stable_pi_suffix

BACKEND = Path(__file__).resolve().parent.parent

PAYLOAD = {
    "customer_name": "Yuki Tanaka",
    "customer_company": "Japan Embroidery Co., Ltd",
    "products": [
        {"name": "gold metallic yarn", "qty": 100, "unit_price": 5.5},
        {"name": "embroidery thread", "qty": 40, "unit_price": 2.25},
    ],
}


def test_the_suffix_is_the_same_in_a_fresh_interpreter():
    """Two processes, one answer. This is the regression that mattered."""
    code = (
        "from app.services.agent_service import _stable_pi_suffix;"
        "print(_stable_pi_suffix('Japan Embroidery Co., Ltd'))"
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=BACKEND, timeout=120,
        )
        for _ in range(2)
    ]
    for run in runs:
        assert run.returncode == 0, run.stderr[-800:]

    assert runs[0].stdout.strip() == runs[1].stdout.strip(), (
        "the suffix changed between interpreters, so a PI number printed in one "
        "process cannot be found by the next"
    )
    assert runs[0].stdout.strip() == str(_stable_pi_suffix("Japan Embroidery Co., Ltd"))


def test_the_suffix_is_four_digits_for_any_name():
    for name in ("", "A", "ACME", "宁波阳光纺织有限公司", "x" * 500):
        value = _stable_pi_suffix(name)
        assert 0 <= value < 10_000


def test_two_runs_of_the_endpoint_agree_on_the_number_and_the_total(client):
    first = client.post("/api/v1/agent/generate-pi", json=PAYLOAD)
    second = client.post("/api/v1/agent/generate-pi", json=PAYLOAD)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    a, b = first.json(), second.json()

    assert re.fullmatch(r"PI-\d{8}-\d{4}", a["pi_number"]), a["pi_number"]
    assert a["pi_number"] == b["pi_number"]
    # 100 x 5.50 + 40 x 2.25
    assert a["total"] == 640.0
    assert b["total"] == 640.0


def test_a_different_company_gets_a_different_number(client):
    first = client.post("/api/v1/agent/generate-pi", json=PAYLOAD).json()
    other = client.post(
        "/api/v1/agent/generate-pi",
        json={**PAYLOAD, "customer_company": "Another Buyer GmbH"},
    ).json()

    assert first["pi_number"].split("-")[-1] != other["pi_number"].split("-")[-1]
