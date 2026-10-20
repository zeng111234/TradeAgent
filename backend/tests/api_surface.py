"""The HTTP surface as a machine-readable contract.

``test_api_contract.py`` freezes which routes exist. That catches a deletion and
nothing else: adding a required field, renaming one, widening a type, changing a
status code or a query parameter's type all leave the route table identical and
the API incompatible. This module reduces ``app.openapi()`` to the part a client
can depend on -- parameters, required fields, types, constraints, status codes --
and stores it next to the test.

What it deliberately drops: descriptions, summaries, examples, tags and model
titles. Those are documentation, and a test that fails when a docstring changes
is a test people delete.

Usage::

    python tests/api_surface.py            # compare, print the diff, exit 1 if changed
    python tests/api_surface.py --update   # accept the change (a visible commit)

The comparison is structural, so the failure names the exact path::

    POST /api/v1/agent/analyze-email.request.schema.properties.email_content.maxLength:
      20000 -> 40000

Whether a change is acceptable is a human decision, not a script's. The policy
is the one in ``test_api_contract.py``: additive changes stay on ``/api/v1``;
anything a current client could break on needs a new path version or a
deprecation window.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SNAPSHOT = Path(__file__).with_name("api_surface.json")

METHODS = ("get", "post", "put", "delete", "patch")

#: Schema keywords that are part of the contract. Anything else (``title``,
#: ``description``, ``examples``) is prose and is dropped on purpose.
_SCHEMA_KEYS = (
    "type",
    "format",
    "enum",
    "maxLength",
    "minLength",
    "minimum",
    "maximum",
    "pattern",
    "nullable",
)


def _resolve(node, spec, seen: tuple[str, ...] = ()):
    """Inline ``$ref``s and keep only contract-relevant keywords."""
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {"$ref": name}  # recursive model: name it, do not recurse
        target = spec.get("components", {}).get("schemas", {}).get(name, {})
        return _resolve(target, spec, seen + (name,))

    out: dict = {}
    for key in _SCHEMA_KEYS:
        if key in node:
            value = node[key]
            out[key] = sorted(value) if key == "enum" else value

    if "required" in node:
        out["required"] = sorted(node["required"])

    if "properties" in node:
        out["properties"] = {
            name: _resolve(value, spec, seen)
            for name, value in sorted(node["properties"].items())
        }

    if "items" in node:
        out["items"] = _resolve(node["items"], spec, seen)

    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            out[key] = [_resolve(value, spec, seen) for value in node[key]]

    return out


def build_surface() -> dict:
    """The live contract, normalised for deterministic comparison."""
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./api-surface-probe.db")
    os.environ["OPENAI_API_KEY"] = ""

    from app.main import app

    spec = app.openapi()
    surface: dict = {}

    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            entry: dict = {}

            parameters = []
            for parameter in operation.get("parameters", []):
                parameters.append({
                    "name": parameter["name"],
                    "in": parameter["in"],
                    "required": bool(parameter.get("required")),
                    "schema": _resolve(parameter.get("schema", {}), spec),
                })
            if parameters:
                entry["parameters"] = sorted(
                    parameters, key=lambda p: (p["in"], p["name"])
                )

            body = operation.get("requestBody")
            if body:
                schema = (
                    body.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                entry["request"] = {
                    "required": bool(body.get("required")),
                    "schema": _resolve(schema, spec) if schema else "untyped",
                }

            responses = {}
            for code, response in operation.get("responses", {}).items():
                schema = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if (
                    code == "422"
                    and isinstance(schema, dict)
                    and schema.get("$ref", "").endswith("HTTPValidationError")
                ):
                    # FastAPI's standard validation envelope. It is identical on
                    # every endpoint and owned by the framework; expanding it
                    # into every operation triples the snapshot without adding a
                    # single client-visible promise.
                    responses[code] = "validation_error"
                    continue
                # ``untyped`` is information, not a gap in this script: it says
                # the endpoint promises nothing about its response body, so no
                # client can claim compatibility from the schema alone.
                responses[code] = _resolve(schema, spec) if schema else "untyped"
            entry["responses"] = dict(sorted(responses.items()))

            surface[f"{method.upper()} {path}"] = entry

    return dict(sorted(surface.items()))


def diff(old, new, prefix: str = "") -> list[str]:
    """A path-precise structural diff; enough to name the breaking line."""
    changes: list[str] = []

    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in old:
                changes.append(f"added {path}: {new[key]!r}")
            elif key not in new:
                changes.append(f"removed {path}: {old[key]!r}")
            else:
                changes.extend(diff(old[key], new[key], path))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            changes.append(f"{prefix}: {old!r} -> {new!r}")
    elif old != new:
        changes.append(f"{prefix}: {old!r} -> {new!r}")

    return changes


def main(argv: list[str]) -> int:
    current = build_surface()

    if "--update" in argv:
        SNAPSHOT.write_text(
            json.dumps(current, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"wrote {SNAPSHOT} ({len(current)} operations)")
        return 0

    previous = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    changes = diff(previous, current)
    if not changes:
        print(f"API surface unchanged ({len(current)} operations)")
        return 0

    print(f"API surface changed ({len(changes)} difference(s)):")
    for line in changes:
        print(f"  - {line}")
    print(
        "\nDecide, do not just update:\n"
        "  additive (new endpoint, new optional field): fine on /api/v1\n"
        "  breaking (removed/renamed field, new required field or parameter,\n"
        "  changed type or status code): add /api/v2 or keep the old shape and\n"
        "  deprecate it with a Sunset header.\n"
        "Then accept the new surface: python tests/api_surface.py --update"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
