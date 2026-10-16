"""Guard against line-ending churn hiding real changes.

The repository stores LF. On Windows the working tree is checked out as CRLF,
so a tool that writes CRLF turns a two-line edit into a whole-file diff. During
the agent refactor this happened twice: a 77/251 change in agent_service.py was
reported as 2650 lines until the endings were normalised, and main.py showed
455 changed lines for a two-line edit.

The first test below is about the stored form (LF in the index), which is what
makes diffs readable. The second is a targeted regression test for the two
files that were actually flipped.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Files that must never be stored with CRLF: everything except the Windows
# scripts, which .gitattributes explicitly pins to CRLF.
CRLF_ALLOWED = {".bat", ".cmd"}


def _ls_files_eol() -> list[tuple[str, str, str]]:
    """Parse ``git ls-files --eol`` into (index, worktree, path) triples."""
    result = subprocess.run(
        ["git", "ls-files", "--eol"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        pytest.skip(f"not a git checkout: {result.stderr.strip()[:120]}")

    entries = []
    for line in result.stdout.splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        fields = meta.split()
        index = next((f[2:] for f in fields if f.startswith("i/")), "")
        worktree = next((f[2:] for f in fields if f.startswith("w/")), "")
        entries.append((index, worktree, path))
    return entries


def test_no_text_file_is_stored_with_crlf():
    offenders = [
        path for index, _worktree, path in _ls_files_eol()
        if index == "crlf" and Path(path).suffix.lower() not in CRLF_ALLOWED
    ]
    assert not offenders, (
        "these files are stored with CRLF, which makes every diff a whole-file "
        "diff; normalise them to LF:\n  " + "\n  ".join(offenders)
    )


def test_no_file_has_mixed_line_endings():
    offenders = [
        path for index, _worktree, path in _ls_files_eol()
        if index == "mixed" or _worktree == "mixed"
    ]
    assert not offenders, "mixed line endings:\n  " + "\n  ".join(offenders)


def test_the_agent_package_is_consistently_lf_on_disk():
    """The files this refactor added, checked directly rather than via git."""
    agent_dir = ROOT / "backend" / "app" / "agent"
    assert agent_dir.is_dir(), "agent package missing"

    offenders = []
    for path in sorted(agent_dir.glob("*.py")):
        count = path.read_bytes().count(b"\r\n")
        if count:
            offenders.append(f"{path.name}: {count} CRLF line ending(s)")
    assert not offenders, "CRLF in the agent package:\n  " + "\n  ".join(offenders)


def test_gitattributes_pins_lf():
    """The rule that keeps this fixed must exist and say the right thing."""
    attributes = ROOT / ".gitattributes"
    assert attributes.exists(), (
        ".gitattributes is missing; without it a CRLF write silently reverts to "
        "whole-file diffs"
    )
    text = attributes.read_text(encoding="utf-8")
    assert "text=auto eol=lf" in text
    assert "*.bat text eol=crlf" in text, "batch files need CRLF to run from cmd.exe"
    assert "*.png binary" in text, "binary files must not have their endings rewritten"


def test_edited_files_do_not_show_a_whole_file_diff():
    """Sanity check on the two files that were actually flipped during the refactor.

    If either is stored as CRLF again, this fails with the reason rather than
    just a large number in `git show --stat`.
    """
    watched = {"backend/app/main.py", "backend/app/database.py",
               "backend/app/services/agent_service.py"}
    stored_crlf = {
        path for index, _w, path in _ls_files_eol()
        if index == "crlf" and path in watched
    }
    assert not stored_crlf, f"stored as CRLF again: {sorted(stored_crlf)}"


#: Everything above checks files git already treats as *text*. Three files in
#: ``app/models`` were none of those, and the whole suite stayed green.


def test_no_text_file_is_mistaken_for_binary():
    """A text file git classifies as binary is a file nothing will ever fix.

    ``text=auto`` only converts files it believes are text. Three files in
    ``app/models`` had every line ending in CR CR LF, which is enough for git to
    call them binary (``-text``) -- and then:

    * git never normalises them,
    * ``git status`` reports them clean, because the working tree matches the
      blob byte for byte,
    * every line-ending test above skips them, because they only look at files
      git already calls text,
    * and Python does not complain, because CR CR LF is whitespace to the
      tokenizer.

    So the corruption was invisible to the repository, to the tests, and to the
    interpreter at the same time.
    """
    text_extensions = {
        ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
        ".js", ".vue", ".css", ".html", ".mmd", ".sh", ".bat", ".cmd",
    }
    offenders = []
    for index, _worktree, path in _ls_files_eol():
        if index != "-text":
            continue
        candidate = ROOT / path
        if not candidate.is_file():
            continue
        if Path(path).suffix.lower() not in text_extensions:
            continue
        if b"\x00" in candidate.read_bytes()[:8000]:
            continue                       # genuinely binary, leave it alone
        offenders.append(path)

    assert not offenders, (
        "git treats these as binary, so nothing will ever normalise them; "
        "rewrite them so they are plain text again:\n  " + "\n  ".join(offenders)
    )


def test_no_file_has_a_doubled_carriage_return():
    """CR CR LF: a shell that turned every LF into CRLF on top of an existing CRLF.

    Checked directly on disk for every tracked text file, because git cannot
    report it: for a file it calls binary, the working tree and the blob agree.
    """
    text_extensions = {
        ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
        ".js", ".vue", ".css", ".html", ".mmd", ".sh",
    }
    offenders = []
    for _index, _worktree, path in _ls_files_eol():
        candidate = ROOT / path
        if not candidate.is_file() or Path(path).suffix.lower() not in text_extensions:
            continue
        doubled = candidate.read_bytes().count(b"\r\r\n")
        if doubled:
            offenders.append(f"{path}: {doubled} line(s)")

    assert not offenders, (
        "doubled carriage returns; `scripts/repair_line_endings.py` fixes these:\n  "
        + "\n  ".join(offenders)
    )
