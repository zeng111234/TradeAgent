"""Every system prompt in the app, by content fingerprint.

Prompts live in git, so git already stores their history. What git does not do
is *notice* on its own: a one-word change to a system prompt leaves all 800+
tests green, because nothing asserts what the instructions say. This module
makes the surface enumerable and the change visible:

* every module-level ``*PROMPT*`` string constant, and
* every ``system="..."`` literal handed to a model call

is reduced to a fingerprint and stored in ``tests/prompt_surface.json``. The
comparison reports three things separately -- a changed prompt, a new one, an
absent one -- because "a prompt was added" is not the same event as "a prompt
was reworded", and both deserve a human look before they ship.

The snapshot stores fingerprints, not the text. Storing the text would make this
file a second copy of the prompts, and a second copy is a copy that can drift
from what runs. The diff of the text is ``git diff``; this file's job is to say
*that* it changed and *which* one.

Usage::

    python tests/prompt_surface.py            # compare, print the diff, exit 1
    python tests/prompt_surface.py --update   # accept the change (a visible commit)
    python tests/prompt_surface.py --list     # fingerprints only

Accepting a change is a deliberate act, not a formality. A prompt edit can move
JSON compliance, tool choice, refusal rate and tone all at once, and none of
those are covered by the test suite -- a green run after ``--update`` means "the
prompt is what you think it is", not "the prompt is better".
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agent.prompts import fingerprint  # noqa: E402

SNAPSHOT = Path(__file__).with_name("prompt_surface.json")


def _module_name(path: Path) -> str:
    relative = path.relative_to(BACKEND).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_text(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _function_names(tree: ast.Module):
    """Yield ``(name, node)`` for every function, including nested ones."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def collect() -> dict[str, str]:
    """All prompts in ``app/``, keyed by a stable name.

    Keys are qualified names rather than line numbers: a line number changes
    when anything above it is edited, which would turn an unrelated refactor
    into "a prompt moved". Function-qualified keys survive that.
    """
    found: dict[str, str] = {}

    for path in sorted((BACKEND / "app").rglob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            name = targets[0].id
            if "PROMPT" not in name or not _is_text(value):
                continue
            found[f"{module}:{name}"] = value.value

        for function_name, function in _function_names(tree):
            seen = 0
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "system" or not _is_text(keyword.value):
                        continue
                    seen += 1
                    suffix = "" if seen == 1 else f"#{seen}"
                    found[f"{module}:{function_name}:system{suffix}"] = keyword.value.value

    return found


def describe(prompts: dict[str, str]) -> dict[str, dict]:
    described = {}
    for key, text in prompts.items():
        lines = [line for line in text.strip().splitlines() if line.strip()]
        described[key] = {
            "fingerprint": fingerprint(text),
            "lines": len(lines),
            "chars": len(text),
            # Enough to recognise the prompt in a failure message without
            # turning the snapshot into a copy of it.
            "opens": lines[0][:60] if lines else "",
        }
    return dict(sorted(described.items()))


def diff(before: dict, after: dict) -> list[str]:
    """Human-readable differences, one line each."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old is None:
            out.append(f"new prompt: {key} ({new['fingerprint']}, {new['lines']} lines)")
        elif new is None:
            out.append(f"removed prompt: {key} (was {old['fingerprint']})")
        elif old["fingerprint"] != new["fingerprint"]:
            out.append(
                f"prompt changed: {key}: {old['fingerprint']} -> {new['fingerprint']} "
                f"({old['lines']} -> {new['lines']} lines)"
            )
    return out


def load() -> dict:
    if not SNAPSHOT.exists():
        return {}
    return json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("prompts", {})


def save(prompts: dict[str, dict]) -> None:
    SNAPSHOT.write_text(
        json.dumps({"prompts": prompts}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str]) -> int:
    current = describe(collect())
    stored = load()

    if "--update" in argv:
        save(current)
        print(f"wrote {len(current)} prompts to {SNAPSHOT.relative_to(BACKEND)}")
        return 0

    if "--list" in argv:
        for key, meta in current.items():
            print(f"{meta['fingerprint']}  {meta['lines']:>3} lines  {key}")
        return 0

    changes = diff(stored, current)
    if not changes:
        print(f"{len(current)} prompts unchanged")
        return 0
    for line in changes:
        print(line)
    print("\nIf the change is intended, run `python tests/prompt_surface.py --update`.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
