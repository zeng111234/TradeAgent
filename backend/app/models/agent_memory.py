"""AgentMemory — a fact the agent chose to keep after a conversation ended.

Why this and not a vector store
-------------------------------
The tempting answer to "it forgets everything between threads" is an embedding
index over every past run. It is the wrong tool for this book. The data is five
customers and a few dozen tasks, it is structured, and the questions asked of it
are exact ones ("which quotes are still open for EuroTextil"). Cosine similarity
would turn a precise lookup into a probabilistic one -- slower, dearer, and able
to miss. The same argument is why ``list_customers`` is SQL.

What was genuinely missing is not *recall*, it is *retention*: nothing survived
the thread. That takes two shapes, and they are different on purpose.

* ``AgentConversation.summary`` is automatic. Every thread compresses itself so
  the next thread knows what was already worked on. Nobody has to remember to
  write it, which is why it will actually be there.
* This table is deliberate. The model decides a sentence is worth keeping --
  a working preference, a standing rule, a fact about how the operator wants
  things done -- and calls a tool. It is small, readable and editable.

Why a person can read every row
-------------------------------
A memory the model wrote and nobody can inspect is a rumour. Every row here is
plain text with a key and a timestamp, so it can be listed in the UI, corrected
by hand, and deleted when it stops being true. Deleting is a real operation,
not an archive flag: a memory that is wrong is worse than no memory.

Business facts do not belong here
---------------------------------
"Customer A's buyer changed" belongs in ``customer_notes``, where it is attached
to the customer, visible on that customer's page, and survives a model change.
A memory row is for what has no home in the schema -- the operator's standing
preferences. The tool descriptions say so, because the model is the one choosing.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class AgentMemory(Base):
    """One durable statement, keyed so it can be updated rather than duplicated."""

    __tablename__ = "agent_memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: What the memory is *about*, short and stable -- "preferred quote currency".
    #: Unique, so writing the same key twice updates instead of piling up two
    #: rows that disagree.
    key = Column(String(80), unique=True, index=True, nullable=False)
    value = Column(String(500), nullable=False)
    #: Which thread taught it. Nullable and not a foreign key: a memory outlives
    #: the conversation, and SQLite cannot add a foreign key by ALTER TABLE.
    source_conversation_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AgentMemory(key={self.key!r})>"
