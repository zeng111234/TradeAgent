"""Sender mailboxes the operator can switch between.

The send path used to read one SMTP account out of the environment, which made
"send from the other mailbox" a file edit and a restart -- fine for one
mailbox, wrong the moment there are two. These rows are that set, with exactly
one active at a time: the service does the switch in a single transaction, and
``ux_smtp_accounts_single_active`` (a partial unique index, migration 0013)
makes two actives impossible even if the service is bypassed.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, text

from app.database import Base


class SmtpAccount(Base):
    """One mailbox the product can send from."""

    __tablename__ = "smtp_accounts"

    # Declared here, not only in migration 0013, so a fresh install's
    # ``create_all`` builds the same schema an upgraded one gets -- otherwise
    # every new database would run a migration it does not need. ``sqlite_where``
    # is what makes it partial: only the active row is constrained, because
    # every inactive row shares the value 0 and a plain unique index would
    # allow one mailbox in total, active or not.
    __table_args__ = (
        Index(
            "ux_smtp_accounts_single_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: What the operator calls it. Shown in the picker; never sent.
    label = Column(String(80), nullable=False)
    host = Column(String(120), nullable=False, default="smtp.qq.com")
    port = Column(Integer, nullable=False, default=465)
    use_tls = Column(Boolean, nullable=False, default=True)
    #: The From address, and the login name. There is one column and not two
    #: because QQ Mail -- the provider this ships configured for -- has no
    #: alias: the address a message claims to be from is the account that
    #: authenticated. A provider that does allow aliases needs a second column,
    #: not a silent mismatch between the two.
    username = Column(String(254), nullable=False)
    #: Stored as given, and never returned over HTTP (the response schema has
    #: no password field). At rest it is exactly as protected as the database
    #: file -- the same trust level .env has always had.
    password = Column(String(200), nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
