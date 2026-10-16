"""Is this service actually asynchronous?

FastAPI is async. That says the framework can run other tasks while a request
awaits -- it says nothing about what the request *does*. Two ways a codebase
quietly fails to be async, and both were present here:

1. **Blocking calls in a coroutine.** A `requests.get`, an `open()`, a
   `time.sleep` inside an `async def` never yields, so the whole event loop stops
   for its duration. The audit below is the permanent version of the scan that
   found the last two (`open()` on the leads file, in the import and preview
   routes); both now run on a worker thread.

2. **Synchronous work that is not a call.** The audit cannot see this, and neither
   can a reader. Measured on this codebase, at 3 000 customers:

       SELECT customers                              0.016s
       load emails/contacts + derive threads         0.203s   (awaited I/O + ORM)
       plan_follow_ups (pure Python)                 0.015s

   So the plan is I/O-shaped, not CPU-shaped -- which corrects an earlier claim of
   mine that it was a CPU block. And the honest headline:

       1 plan run                  0.282s
       4 plan runs concurrently    1.984s   (about 2x the sequential 1.0s)
       50 trivial reads sequential 0.031s
       50 trivial reads concurrent 0.063s

   **Concurrency here makes things slower, not faster.** Not what "async" is sold
   as. `journal_mode=WAL` -- SQLite's usual answer -- changed it by 3% (2.05s ->
   1.98s), so the journal is not the dominant cause; the candidates left are
   per-connection cost, single-threaded row-to-object work, and one writer. I did
   not isolate which, and this file does not pretend otherwise.

The pragmas are asserted here because they are a property of the engine that a
refactor could silently drop.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

#: APIs that stop the event loop when called inside a coroutine. Kept as
#: qualified names so a guard can be specific about what it forbids.
BLOCKING_CALLS = {
    "time.sleep": "sleeps the loop",
    "requests.get": "blocking HTTP",
    "requests.post": "blocking HTTP",
    "requests.put": "blocking HTTP",
    "requests.delete": "blocking HTTP",
    "requests.request": "blocking HTTP",
    "urlopen": "blocking HTTP",
    "smtplib.SMTP": "blocking SMTP",
    "socket.create_connection": "blocking socket",
    "subprocess.run": "blocking process",
    "os.system": "blocking process",
    "os.popen": "blocking process",
    "open": "blocking file I/O",
    "read_text": "blocking file I/O",
    "write_text": "blocking file I/O",
    "read_bytes": "blocking file I/O",
    "write_bytes": "blocking file I/O",
    "json.load": "blocking file I/O",
    "json.dump": "blocking file I/O",
}

#: The one place a blocking call may sit inside a coroutine, with the reason.
#: Adding to this list is the intended way to proceed -- and it should feel like
#: a decision, because it is one.
ALLOWED: dict[str, str] = {}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _BlockingInCoroutines(ast.NodeVisitor):
    """Walk each `async def`, skipping nested sync functions.

    Skipping nested `def`s is what makes the `to_thread` pattern legal: the
    blocking code lives in a sync function that is handed to the thread pool, and
    that function is the fix rather than the problem.
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.stack: list[str] = []
        self.findings: list[str] = []

    def visit_AsyncFunctionDef(self, node):
        self.stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        if not self.stack:
            self.generic_visit(node)

    def visit_Call(self, node):
        name = _dotted(node.func)
        hint = BLOCKING_CALLS.get(name) or BLOCKING_CALLS.get(name.split(".")[-1])
        key = f"{self.path.name}:{node.lineno}"
        if hint and self.stack and key not in ALLOWED:
            try:
                shown = self.path.relative_to(APP.parent)
            except ValueError:  # a synthetic path in the tests below
                shown = self.path
            self.findings.append(
                f"{shown}:{node.lineno} -- "
                f"{name}() inside async {'.'.join(self.stack)}(): {hint}. "
                "Move it into a plain `def` and call it with asyncio.to_thread."
            )
        self.generic_visit(node)


def _python_files() -> list[pathlib.Path]:
    return [p for p in sorted(APP.rglob("*.py")) if "__pycache__" not in p.parts]


def test_no_coroutine_in_the_application_blocks_the_event_loop():
    findings: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scan = _BlockingInCoroutines(path)
        scan.visit(tree)
        findings.extend(scan.findings)

    assert not findings, (
        "a blocking call inside a coroutine stops every other request:\n  "
        + "\n  ".join(findings)
    )


def test_the_scan_finds_a_blocking_call():
    """A guard nobody has seen fail is a guard nobody should trust."""
    sample = '''
async def handler():
    response = requests.get("https://example.test")
    return response.text
'''
    tree = ast.parse(sample)
    scan = _BlockingInCoroutines(pathlib.Path("sample.py"))
    scan.visit(tree)

    assert len(scan.findings) == 1
    assert "requests.get" in scan.findings[0]


def test_the_scan_allows_the_worker_thread_pattern():
    """`asyncio.to_thread` with a plain function is the fix, not a finding."""
    sample = '''
async def handler():
    def _fetch():
        with open("somewhere") as handle:
            return handle.read()
    return await asyncio.to_thread(_fetch)
'''
    tree = ast.parse(sample)
    scan = _BlockingInCoroutines(pathlib.Path("sample.py"))
    scan.visit(tree)

    assert scan.findings == []


def test_the_database_is_configured_for_concurrent_readers():
    """WAL is a setting a refactor can drop without anything failing.

    It did not fix the concurrency measurement above, and saying so matters --
    but without it a writer blocks every reader for the length of its
    transaction, and this application writes progress while serving requests.

    Read through a real connection rather than by inspecting the registry: the
    pragma is the property that matters, and journal mode is visible from SQL.
    """
    import asyncio

    from sqlalchemy import text

    from app.database import engine

    assert engine.dialect.name == "sqlite"

    async def read():
        async with engine.connect() as conn:
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            sync_mode = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
            return journal, sync_mode

    journal, sync_mode = asyncio.run(read())

    assert journal.lower() == "wal", (
        "readers are blocked by writers without WAL, and this application writes "
        "progress while serving requests"
    )
    # 1 == NORMAL, the documented companion to WAL.
    assert int(sync_mode) == 1
