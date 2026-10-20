"""The prompt surface is pinned; a change has to be acknowledged.

Nothing else in the suite asserts what the instructions say. Without this, a
one-word edit to ``DEFAULT_SYSTEM_PROMPT`` is indistinguishable from no edit at
all: every other test drives the loop with a fake model that ignores the prompt,
so it would pass either way.
"""
from __future__ import annotations

import prompt_surface
from app.agent.prompts import fingerprint


def test_the_prompt_surface_matches_the_snapshot():
    current = prompt_surface.describe(prompt_surface.collect())
    stored = prompt_surface.load()
    changes = prompt_surface.diff(stored, current)

    assert changes == [], (
        "the prompt surface changed. Read `git diff backend/app`, decide whether "
        "the change is intended, then run:\n"
        "    python tests/prompt_surface.py --update\n"
        "A green suite after accepting only means the prompt is what you think "
        "it is -- not that it is better.\n\n" + "\n".join(changes)
    )


def test_the_snapshot_covers_every_prompt_in_the_app():
    """A new prompt is an event, not a detail.

    Counting rather than listing: the point is that the snapshot cannot quietly
    stop covering a file because a path moved or a constant was renamed.
    """
    current = prompt_surface.collect()
    stored = prompt_surface.load()

    assert len(current) == len(stored), (
        f"{len(current)} prompts in app/, {len(stored)} in the snapshot"
    )
    assert len(current) >= 13, "prompts disappeared; check the scanner, not the count"


def test_every_prompt_has_a_twelve_character_identity():
    for key, text in prompt_surface.collect().items():
        identity = fingerprint(text)
        assert len(identity) == 12, key
        assert all(c in "0123456789abcdef" for c in identity), key


def test_identity_ignores_invisible_whitespace_but_not_words():
    """The normalisation rule the fingerprint promises.

    Trailing spaces are invisible in review and change no instruction, so they
    must not mint a new identity. A double space inside a sentence is visible in
    the text and does change it, so it must.
    """
    assert fingerprint("line one\nline two   \n") == fingerprint("line one\nline two")
    assert fingerprint("line one\n\n") == fingerprint("line one")
    assert fingerprint("line one") != fingerprint("line  one")
    assert fingerprint("line one") != fingerprint("line two")


def test_the_diff_separates_changed_added_and_removed():
    before = {
        "a": {"fingerprint": "111111111111", "lines": 1, "chars": 10, "opens": "old"},
        "gone": {"fingerprint": "222222222222", "lines": 2, "chars": 20, "opens": "bye"},
    }
    after = {
        "a": {"fingerprint": "333333333333", "lines": 3, "chars": 30, "opens": "new"},
        "added": {"fingerprint": "444444444444", "lines": 1, "chars": 12, "opens": "hi"},
    }

    assert prompt_surface.diff(before, after) == [
        "prompt changed: a: 111111111111 -> 333333333333 (1 -> 3 lines)",
        "new prompt: added (444444444444, 1 lines)",
        "removed prompt: gone (was 222222222222)",
    ]
