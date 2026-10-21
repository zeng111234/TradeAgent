"""Which mailbox a send goes out from, and the switching between them.

One account is active at a time. The switch is a single transaction that
deactivates everyone else and activates the chosen one, and a partial unique
index backs it up at the database level -- so "two active mailboxes" is not a
state this service can produce even by racing itself.

With no account active the send path falls back to the environment variables,
which is where the configuration lived before this table existed. That order
is deliberate: an operator who has not touched this page sees exactly the
behaviour they had yesterday, and an operator who adds a mailbox gets it
without editing a file.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.smtp_account import SmtpAccount


@dataclass(frozen=True)
class ResolvedSender:
    """Everything a send needs, from whichever source won.

    ``source`` is for logs and the status endpoint: "which mailbox did that
    leave from" is a question worth being able to answer after the fact. The
    password travels with it because the SMTP handshake needs it, and goes no
    further -- no response schema carries it.
    """

    host: str
    port: int
    use_tls: bool
    username: str
    password: str
    source: str  # "account" | "env"
    #: Which row produced this, when a row did. None for the environment
    #: fallback, which has no id to point at.
    account_id: int | None = None


def sender_from_env() -> ResolvedSender | None:
    """The pre-table configuration, still honoured when no row is active."""
    if not (settings.SMTP_USER and settings.SMTP_PASSWORD):
        return None
    return ResolvedSender(
        host=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        use_tls=settings.SMTP_USE_TLS,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        source="env",
    )


async def resolve_active_sender(db: AsyncSession) -> ResolvedSender | None:
    """The account a send would use now, or None when nothing is configured.

    None is not an error: it is the state that makes a send simulated, which
    the send path already knows how to report honestly.
    """
    result = await db.execute(
        select(SmtpAccount).where(SmtpAccount.is_active.is_(True))
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return ResolvedSender(
            host=row.host,
            port=row.port,
            use_tls=row.use_tls,
            username=row.username,
            password=row.password,
            source="account",
            account_id=row.id,
        )
    return sender_from_env()


async def list_accounts(db: AsyncSession) -> list[SmtpAccount]:
    """Active first, then oldest -- the active one is the one to see first."""
    result = await db.execute(
        select(SmtpAccount).order_by(SmtpAccount.is_active.desc(), SmtpAccount.id.asc())
    )
    return list(result.scalars().all())


async def create_account(
    db: AsyncSession, data, activate: bool = False
) -> SmtpAccount:
    """Add a mailbox. ``activate`` switches to it in the same breath."""
    row = SmtpAccount(
        label=data.label,
        host=data.host,
        port=data.port,
        use_tls=data.use_tls,
        username=data.username,
        password=data.password,
        is_active=False,
    )
    db.add(row)
    await db.flush()
    if activate:
        await _switch_to(db, row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_account(db: AsyncSession, account_id: int, data) -> SmtpAccount | None:
    """Edit a mailbox. An omitted password keeps the stored one."""
    row = await db.get(SmtpAccount, account_id)
    if row is None:
        return None
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


async def activate_account(db: AsyncSession, account_id: int) -> SmtpAccount | None:
    """Make this the one account a send uses. None when the id is unknown."""
    row = await db.get(SmtpAccount, account_id)
    if row is None:
        return None
    await _switch_to(db, row)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_account(db: AsyncSession, account_id: int) -> bool:
    """Remove a mailbox. Deleting the active one leaves none active.

    It does not silently promote another: the next send then falls back to the
    environment, and if that is unconfigured it is simulated -- both of which
    the status endpoint and the send path say out loud. Quietly sending from
    a different mailbox than the one the operator deleted would be worse.
    """
    row = await db.get(SmtpAccount, account_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def _switch_to(db: AsyncSession, row: SmtpAccount) -> None:
    """Deactivate everyone else, then this one -- one flush, one commit.

    The bulk UPDATE runs first so that even a unique index that only the
    database enforces sees the old active row gone before the new one lands.
    """
    await db.execute(
        update(SmtpAccount)
        .where(SmtpAccount.is_active.is_(True), SmtpAccount.id != row.id)
        .values(is_active=False)
    )
    row.is_active = True
    await db.flush()
