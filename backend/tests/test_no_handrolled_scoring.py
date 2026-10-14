"""Guards against the hand-rolled scorer coming back.

The single most important behaviour change in this refactor is that prospect
ranking is no longer computed by a formula in Python. That is easy to
reintroduce by accident while "optimising", so it is asserted rather than
trusted.

The check is AST-based, not textual. Several docstrings deliberately quote the
removed code to explain what was wrong with it, and a grep would flag those.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_ROOTS = [ROOT / "backend" / "app", ROOT / "scripts"]

# The exact constants that used to be added up. If any of these appears as an
# augmented assignment to a score-like name, the heuristic is back.
FORMER_SCORE_CONSTANTS = {5, 10, 15, 20, 30}


def _read(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError(f"cannot decode {path}")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for base in PYTHON_ROOTS:
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def test_there_are_python_files_to_check():
    assert len(_python_files()) > 5


def test_no_score_variable_is_incremented_by_a_magic_number():
    """``relevance_score += 30`` and friends must not exist in executable code.

    Docstrings are ignored because ``ast`` does not descend into them as code.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AugAssign):
                continue
            target = ast.unparse(node.target).lower()
            if "score" not in target:
                continue
            if isinstance(node.value, ast.Constant) and node.value.value in FORMER_SCORE_CONSTANTS:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {ast.unparse(node)}")
            elif not isinstance(node.value, ast.Constant):
                # A non-constant increment is still a running tally; suspicious
                # enough to review, and cheap to whitelist if legitimate.
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} {ast.unparse(node)} (non-constant)"
                )
    assert not offenders, (
        "a running score tally reappeared; ranking must be a model judgement:\n  "
        + "\n  ".join(offenders)
    )


def test_no_platform_boost_list_exists():
    """The B2B-platform bonus list was the largest single constant."""
    for path in _python_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                name = ast.unparse(target).lower()
                if "b2b" in name and "platform" in name:
                    raise AssertionError(
                        f"{path.relative_to(ROOT)}:{node.lineno} reintroduced a B2B "
                        "platform boost list"
                    )


def test_the_old_scanner_functions_are_gone():
    """The helpers that implemented the removed pipeline must not return."""
    removed = {
        "search_leads",
        "scrape_lead_details",
        "generate_draft_email",
        "load_scanned_domains",
    }
    for path in _python_files():
        tree = ast.parse(_read(path))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        overlap = defined & removed
        assert not overlap, (
            f"{path.relative_to(ROOT)} redefines {sorted(overlap)}; these belonged to the "
            "hardcoded scan that the agent replaced"
        )


def test_auto_lead_scanner_has_no_network_or_scoring_body():
    """It should be a delegation, not an implementation."""
    path = ROOT / "backend" / "app" / "services" / "agent_service.py"
    tree = ast.parse(_read(path))

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "auto_lead_scanner"),
        None,
    )
    assert fn is not None, "auto_lead_scanner disappeared; the endpoint contract needs it"

    body_source = ast.unparse(fn)
    for banned in ("requests.get", "BeautifulSoup", "DDGS", "chat.completions.create"):
        assert banned not in body_source, (
            f"auto_lead_scanner contains {banned!r}; the scraping and scoring belonged to "
            "the agent path"
        )
    assert "find_leads_agentic" in body_source, "auto_lead_scanner no longer delegates to the agent"


def test_basic_website_analysis_produces_no_lead_score():
    """The no-model fallback must not invent a score it cannot justify."""
    path = ROOT / "backend" / "app" / "services" / "agent_service.py"
    tree = ast.parse(_read(path))

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_basic_analyze_website"),
        None,
    )
    assert fn is not None

    source = ast.unparse(fn)
    assert "lead_score" not in source, (
        "_basic_analyze_website is producing a lead_score again. A keyword-derived number "
        "is displayed as an assessment but is not one."
    )
    # And it must be honest about having assessed nothing.
    returned = next(
        (n for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)),
        None,
    )
    assert returned is not None
    keys = [
        k.value for k in returned.value.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    ]
    assert "not_analyzed" in keys


@pytest.mark.parametrize(
    "relpath",
    ["backend/app/agent/trade_tools.py", "backend/app/agent/scan_tools.py"],
)
def test_agent_tools_contain_no_scoring_formula(relpath):
    path = ROOT / relpath
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and "score" in ast.unparse(node.target).lower():
            raise AssertionError(f"{relpath}:{node.lineno} scores with {ast.unparse(node)}")
