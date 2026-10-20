"""Prompt identity.

Prompts are code here: they live in git, they are reviewed as a diff, and they
are rolled back with the code that parses their output. Keeping them in a
database or a prompt-management service would put the instructions and the
parser in two places that can disagree, and the failure mode is subtle -- a
prompt that asks for a field the parser stopped reading, or a parser that still
handles a shape the prompt no longer produces.

What git cannot answer is *which* prompt produced a row in ``agent_runs``. The
row outlives the commit; the working tree only ever holds one version. So a
prompt's identity is its content: a short SHA-256 fingerprint, recorded on every
run, so two runs can be compared without guessing what the deployment was
running at the time.

Deliberately absent:

* **Version numbers.** ``v3`` is a promise that somebody bumps it every time,
  and the one time nobody does, the number is worse than no number: it looks
  authoritative and is wrong. A content hash cannot be forgotten.
* **A prompt registry or table.** A second copy of the prompts, with its own
  lifecycle, is a copy that can drift from what actually runs.
* **Runtime selection.** This deployment runs one prompt at a time. Choosing
  between prompts per request is an experiment, and an experiment needs a
  metric; without one it is a coin flip with extra steps.

The fingerprint covers the **static instructions** only. Per-run context is
appended as a separate system message by :class:`AgentLoop`, and it is data, not
code -- versioning it would mean versioning the customer book.
"""
from __future__ import annotations

import hashlib


def fingerprint(text: str) -> str:
    """Content identity of a prompt: 12 hex characters of SHA-256.

    Twelve hex characters is 48 bits. At a few hundred distinct prompts a
    collision is not a realistic concern, and it is short enough to read in a
    row, a log line and a diff.

    Whitespace is normalised first: trailing spaces on a line are invisible in
    review and change nothing about the instructions, so they must not mint a
    new identity. Any change to the text itself does.
    """
    normalised = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]
