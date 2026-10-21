"""AgentConversation — one thread, so a follow-up knows what came before.

A run is a single goal. A conversation is the reason two runs belong together:
the second question says "and that customer?" and only the thread can tell the
model which customer. The table holds almost nothing on purpose -- the turns
themselves live on ``agent_runs``, linked by ``conversation_id``, so there is
one record of a run rather than a copy of it here.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class AgentConversation(Base):
    """The identity and title of a chat thread."""

    __tablename__ = "agent_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), unique=True, index=True, nullable=False)
    #: The first goal, truncated. Auto-derived rather than asked for: a title
    #: prompt is one more model call the user did not request.
    title = Column(String(120), nullable=False, default="")
    #: The customer the thread is about, when every run in it named one. Nullable
    #: and not a foreign key: SQLite cannot add one by ``ALTER TABLE``, and the
    #: row stays useful if the customer is deleted.
    customer_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AgentConversation(id={self.id}, conversation_id='{self.conversation_id}')>"
