"""AgentRun — the audit trail for every autonomous execution.

One row per run, holding the full trace as JSON. This is what makes the agent
accountable: you can see the goal, every tool it chose, the arguments it
passed, what came back, and why it stopped. Nothing about the run is
reconstructed after the fact.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.database import Base


class AgentRun(Base):
    """A single agent execution, start to finish."""

    __tablename__ = "agent_runs"
    #: A unique key only when one was sent. Both SQLite and PostgreSQL treat
    #: NULLs as distinct in a unique index, so the rows that never used the
    #: header do not collide with each other.
    __table_args__ = (
        Index("ix_agent_runs_idempotency_key", "idempotency_key", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, index=True, nullable=False)

    #: Which customer this run was about, when the caller said so.
    #:
    #: Added by migration ``0001_agent_runs_customer_id``. The column did not
    #: exist before, which is why the debug view found runs by scanning their
    #: JSON trace for a customer id -- precise, because it parses the arguments,
    #: but unindexable and blind to a run that named a customer only in prose.
    #: Nullable on purpose: plenty of runs are about the whole book rather than
    #: one customer, and rows written before the column have no value to invent.
    customer_id = Column(
        Integer, ForeignKey("customers.id"), nullable=True, index=True
    )

    #: Fingerprint of the system prompt this run used (12 hex chars).
    #:
    #: Added by migration ``0006_agent_runs_prompt_version``. The trace records
    #: what the model *did*; this records what it was *told*. Without it a change
    #: in behaviour cannot be attributed to a prompt edit rather than to the
    #: model, the data, or the tools -- and a run produced by instructions that
    #: are no longer in the tree cannot be told apart from one produced today.
    #: Nullable: rows written before the column existed have nothing to invent.
    prompt_version = Column(String(32), nullable=True)

    #: Caller-supplied key that makes "start a run" safe to retry.
    #:
    #: Added by migration ``0007_agent_runs_idempotency_key``. A run costs money
    #: and writes to the same book a human is reading, so a client that times out
    #: and retries must not start a second one. Unique when present; NULL for the
    #: calls that did not send the header, which is most of them.
    idempotency_key = Column(String(120), nullable=True)

    goal = Column(Text, nullable=False)
    #: The thread this run belongs to. Added by migration 0011; nullable because
    #: rows written before threads existed were genuinely independent.
    conversation_id = Column(String(64), nullable=True, index=True)
    #: The model that actually produced this run. Added by migration 0012.
    #: Nullable for the same reason: the old rows have no honest value.
    model = Column(String(80), nullable=True)
    # running | succeeded | truncated | error | stalled | cancelled
    status = Column(String(32), nullable=False, default="running")
    #: Machine-readable reason the run stopped.
    #:
    #: Added by migration ``0008_agent_runs_stop_code``. ``stop_reason`` is the
    #: sentence a human reads; it merged three different ``truncated`` causes
    #: (time, rounds, tokens) into prose that could not be grouped. Nullable
    #: because rows written before the column have no code to invent, and the
    #: sentence remains the source of truth for those.
    stop_code = Column(String(32), nullable=True, index=True)
    stop_reason = Column(String(300), nullable=True)
    error = Column(Text, nullable=True)

    final_output = Column(Text, nullable=True)

    rounds = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    #: Of ``prompt_tokens``, how many came from the provider's prefix cache.
    #: Added by migration ``0002_agent_runs_cached_prompt_tokens``: a run's input
    #: is not one price, and the row could not say which part was cheap.
    cached_prompt_tokens = Column(Integer, default=0)
    elapsed = Column(Integer, default=0)          # milliseconds

    # JSON: list of TraceStep.as_dict()
    trace = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AgentRun(id={self.id}, run_id='{self.run_id}', status='{self.status}')>"
