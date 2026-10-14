"""Every source file must be UTF-8.

Three model files were authored as GB18030. They imported fine — CPython's
tokenizer uses surrogateescape for source, so undecodable bytes confined to
comments and string literals do not raise — which is exactly why the problem
survived: nothing failed, the Chinese comments just rendered as mojibake on any
system whose locale is not cp936, including every Linux container and CI runner.

The same class of bug bit harder once: a single em dash written as GB18030
multi-byte inside a *string literal* is rejected by the tokenizer, and it broke
`import app.main` outright — the whole application failed to start, with every
test still green because none of them imported that line.

So this asserts the encoding directly rather than trusting it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Text that must be UTF-8. Binaries are excluded; .bat/.cmd are handled by
# .gitattributes for line endings, not encoding.
SOURCES = ["backend/app", "backend/tests", "scripts", "frontend/src", "docs"]
SUFFIXES = {".py", ".js", ".mjs", ".vue", ".md", ".json", ".yml", ".yaml", ".css", ".html"}
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "dist", ".pytest_cache"}


def _files() -> list[Path]:
    found: list[Path] = []
    for base in SOURCES:
        root = ROOT / base
        if not root.exists():
            continue
        found.extend(
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUFFIXES
            and not any(part in SKIP_PARTS for part in path.parts)
        )
    return sorted(found)


def test_there_are_files_to_check():
    assert len(_files()) > 20


def test_every_source_file_is_valid_utf8():
    offenders = []
    for path in _files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{path.relative_to(ROOT)} ({exc.reason} at byte {exc.start})")

    assert not offenders, (
        "these files are not UTF-8, so their comments are unreadable off Windows "
        "and a multi-byte byte inside a string literal will break the import:\n  "
        + "\n  ".join(offenders)
    )


def test_no_python_file_declares_a_non_utf8_encoding():
    """A PEP 263 declaration would make a GB18030 file legal. We do not want that."""
    offenders = []
    for path in _files():
        if path.suffix != ".py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines()[:2], 1):
            if "coding:" in line or "coding=" in line:
                coding = line.split("coding")[1].lstrip(":=").strip().split()[0]
                if "utf-8" not in coding.lower() and "utf8" not in coding.lower():
                    offenders.append(f"{path.relative_to(ROOT)}:{number} declares {coding}")
    assert not offenders, "non-UTF-8 encoding declared:\n  " + "\n  ".join(offenders)


def test_the_model_package_parses_and_keeps_its_names():
    """The three converted files must still define everything they defined."""
    import app.models as models

    for name in ("Customer", "CustomerContact", "CustomerNote",
                 "EmailLog", "EmailTemplate", "Task", "AgentRun"):
        assert hasattr(models, name), f"app.models lost {name}"


def test_converted_files_kept_their_non_ascii_comments():
    """Conversion must preserve the comments, not strip them.

    The Chinese labels are the documentation for what each enum value means; a
    conversion that silently dropped them would look like a successful conversion.
    """
    customer = (ROOT / "backend" / "app" / "models" / "customer.py").read_text(encoding="utf-8")
    tree = ast.parse(customer)

    # Enum member comments live in the source, not the AST, so check the text.
    for label in ("新客户", "已联系", "有意向", "报价中", "寄样中", "已成交", "已流失"):
        assert label in customer, f"lost the comment for a pipeline stage: {label}"

    # And the enums themselves are intact.
    stages = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CustomerStage"
    ][0]
    assert len(stages.body) >= 8, "CustomerStage lost members"


def test_no_source_file_has_stray_surrogates():
    """Decoding with surrogateescape can hide damage; utf-8 strict must be clean.

    This is what actually broke `import app.main`: bytes that decode under
    surrogateescape but are rejected by the tokenizer when they land inside a
    string literal.
    """
    offenders = []
    for path in _files():
        if path.suffix != ".py":
            continue
        raw = path.read_bytes()
        # surrogateescape is what CPython falls back to; if those bytes exist,
        # the file is not genuinely UTF-8 even though it "loads".
        decoded = raw.decode("utf-8", errors="surrogateescape")
        if any(0xDC80 <= ord(ch) <= 0xDCFF for ch in decoded):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "files contain undecodable bytes:\n  " + "\n  ".join(offenders)
