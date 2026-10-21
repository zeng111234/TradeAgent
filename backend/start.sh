#!/bin/sh
# Entrypoint for a hosted deployment.
#
# Two things have to be pinned here rather than left to the environment, and
# both were learned the hard way:
#
# 1. DATABASE_URL. A host that injects its own database URL wins over the
#    value in .env, and this app ships with no driver for anything but
#    SQLite -- so it is stated here, as an absolute path, instead of being
#    inherited from whatever the sandbox happens to provide.
# 2. The port. The platform passes it as PORT; uvicorn must be told, and a
#    literal default only helps when the variable is missing.
#
# The probe runs before the server and *stops the script* when the database
# cannot be opened. A deployment that fails at import shows a truncated
# traceback, and the one line that explains it -- the exception itself -- is
# the line that gets cut; failing here puts that line last, where it survives.
set -u

export DATABASE_URL="sqlite+aiosqlite:////workspace/tradeagent.db"
export DEBUG=false

echo "python: $(python -V 2>&1)" >&2
# Everything goes to stderr: the deploy log collects stderr, not stdout, so a
# diagnostic printed to stdout is a diagnostic nobody ever reads.
python - <<'PY'
import os
import sys

import aiosqlite
import sqlalchemy


def say(*parts):
    print(*parts, file=sys.stderr, flush=True)


say("sqlalchemy", sqlalchemy.__version__, "| aiosqlite", aiosqlite.__version__)
say("DATABASE_URL", os.environ.get("DATABASE_URL"))
try:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_timeout=5.0,
        connect_args={"timeout": 30},
    )
    say("engine ok:", engine.pool.__class__.__name__)
except Exception as exc:
    say("ENGINE_ERROR:", type(exc).__name__, exc)
    sys.exit(1)
PY

[ $? -eq 0 ] || exit 1

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
