"""AgentConversation — one thread, so a follow-up knows what came before.

A run is a single goal. A conversation is the reason two runs belong together:
the second question says "and that customer?" and only the thread can tell the
model which customer. The table holds almost nothing on purpose -- the turns
themselves live on ``agent_runs``, linked by ``conversation_id``, so there is
one record of a run rather than a copy of it here.

The three ``summary*`` columns are the exception, and they are what makes a
*new* thread aware of an old one. They hold a rolling, model-written digest of
the thread (what was asked, what was concluded, what is still owed), rebuilt
incrementally so its cost stays flat as the thread grows. See
``app/agent/memory.py`` for why a digest and not an embedding index.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

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

    #: The thread in a paragraph: what was wanted, what was found, what is left.
    #: Written by the model, read by the next thread. Null until the first run
    #: finishes, and left null when summarising fails -- an empty digest is a
    #: better outcome than a wrong one.
    summary = Column(Text, nullable=True)
    #: How many concluded runs the digest already covers. The rebuild feeds only
    #: what is newer than this, so the prompt does not grow with the thread.
    summary_turns = Column(Integer, default=0, nullable=False)
    #: When the digest was last rebuilt. For display and for the staleness test.
    summarized_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AgentConversation(id={self.id}, conversation_id='{self.conversation_id}')>"
