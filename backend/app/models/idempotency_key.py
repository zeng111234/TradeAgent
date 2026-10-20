"""One row per idempotency key, shared by every create endpoint.

Why not a column on each table
------------------------------
Five endpoints means five columns, and a sixth means another migration. The key
belongs to *a request*, not to a customer or a task, and the request is already
identified by ``(scope, key)``. A table makes that relationship data instead of
schema.

What is stored, and what is deliberately not
--------------------------------------------
``request_hash`` is the fingerprint of the body that claimed the key. It is what
turns "the same key twice" into two different answers:

* same key, same body -> replay the original row;
* same key, different body -> 409, because the caller is reusing a key for a
  different request, not retrying.

``response_id`` points at the row that was created. The alternative -- storing
the serialised response -- would freeze a schema at write time and replay an old
shape after the API changes. Pointing at the row means a replay reads today's
shape, and a row that has since been deleted is an honest 409 rather than a
resurrection.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String

from app.database import Base


class IdempotencyKey(Base):
    """A claimed ``(scope, key)`` and the row it produced."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # A named unique *index*, not a UniqueConstraint: SQLite names the
        # index behind a table constraint ``sqlite_autoindex_...``, which the
        # migration's guard could not see. Naming it makes a fresh
        # ``create_all`` and an upgraded database produce the same object.
        Index("uq_idempotency_scope_key", "scope", "key", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Which create endpoint claimed it: ``customer``, ``customer_contact``,
    #: ``customer_note``, ``task``, ``email_template``. Keys are scoped so the
    #: same client-generated value can be used for different resources.
    scope = Column(String(40), nullable=False)
    key = Column(String(120), nullable=False)
    request_hash = Column(String(64), nullable=False)
    response_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<IdempotencyKey(scope='{self.scope}', key='{self.key}')>"
