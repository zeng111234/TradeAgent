"""Repair three files whose every line ends with CR CR LF.

They were rewritten by a shell that turned each ``\\n`` into ``\\r\\n`` on top of
an existing ``\\r\\n``. Git then classified them as binary (``-text``), which
means it never normalises them, ``git status`` never reports them, and the
line-ending test never looks at them because that test only inspects files git
already treats as text. Python does not care -- ``\\r\\r\\n`` is whitespace to
the tokenizer -- so nothing failed.

This rewrites the endings and nothing else. Run with --check to only report.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "backend" / "app" / "models" / "customer.py",
    ROOT / "backend" / "app" / "models" / "email_log.py",
    ROOT / "backend" / "app" / "models" / "email_template.py",
]


def repair(raw: bytes) -> bytes:
    """CR CR LF -> LF, and any remaining CR LF -> LF."""
    return raw.replace(b"\r\r\n", b"\n").replace(b"\r\n", b"\n")


def main() -> int:
    check_only = "--check" in sys.argv
    problems = 0

    for path in TARGETS:
        raw = path.read_bytes()
        fixed = repair(raw)
        doubled = raw.count(b"\r\r\n")
        print(f"{path.relative_to(ROOT)}")
        print(f"   doubled CRLF lines : {doubled}")
        print(f"   bytes              : {len(raw)} -> {len(fixed)}")
        if b"\x00" in raw:
            print("   NUL byte present: this really is binary, leaving it alone")
            continue
        if fixed == raw:
            print("   already clean")
            continue
        problems += 1
        if not check_only:
            path.write_bytes(fixed)
            print("   rewritten with LF endings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
