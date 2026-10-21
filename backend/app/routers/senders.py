"""Sender mailboxes: the accounts a send can go out from.

This is the operator's page, not the agent's. The agent has no tool that
touches these rows -- which mailbox sends is a decision about identity, and
identity is not something a tool call gets to change. What the agent is
allowed to do is unchanged: it prepares drafts, a person sends them, and the
mailbox they leave from is the one active here.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.errors import ApiError, ErrorCode
from app.models.smtp_account import SmtpAccount
from app.schemas.smtp_account import (
    SmtpAccountCreate,
    SmtpAccountResponse,
    SmtpAccountUpdate,
    SenderStatus,
)
from app.services import smtp_accounts

router = APIRouter(prefix="/senders", tags=["Senders"])


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code=ErrorCode.SENDER_NOT_FOUND,
        detail="sender mailbox not found",
    )


@router.get("", response_model=list[SmtpAccountResponse])
async def list_senders(db: AsyncSession = Depends(get_db)):
    """Every mailbox, active first. Passwords are not in the response."""
    return await smtp_accounts.list_accounts(db)


@router.get("/effective", response_model=SenderStatus)
async def effective_sender(db: AsyncSession = Depends(get_db)):
    """What a send would use right now, without sending anything.

    ``simulated`` is an honest answer, not an error: it is what happens with
    no active account and no credentials in the environment, and the send path
    labels those rows so they cannot later be mistaken for real deliveries.
    """
    sender = await smtp_accounts.resolve_active_sender(db)
    return SenderStatus(
        effective=sender.source if sender else "simulated",
        active_account_id=sender.account_id if sender else None,
        env_configured=bool(settings.SMTP_USER and settings.SMTP_PASSWORD),
    )


@router.post("", response_model=SmtpAccountResponse, status_code=201)
async def create_sender(data: SmtpAccountCreate, db: AsyncSession = Depends(get_db)):
    """Add a mailbox, optionally switching to it immediately."""
    return await smtp_accounts.create_account(db, data, activate=data.activate)


@router.put("/{account_id}", response_model=SmtpAccountResponse)
async def update_sender(
    account_id: int, data: SmtpAccountUpdate, db: AsyncSession = Depends(get_db)
):
    """Edit a mailbox. An omitted password keeps the stored one."""
    row = await smtp_accounts.update_account(db, account_id, data)
    if row is None:
        raise _not_found()
    return row


@router.post("/{account_id}/activate", response_model=SmtpAccountResponse)
async def activate_sender(account_id: int, db: AsyncSession = Depends(get_db)):
    """Switch: this mailbox becomes the one every send uses."""
    row = await smtp_accounts.activate_account(db, account_id)
    if row is None:
        raise _not_found()
    return row


@router.delete("/{account_id}", status_code=204)
async def delete_sender(account_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a mailbox. Deleting the active one leaves none active."""
    if not await smtp_accounts.delete_account(db, account_id):
        raise _not_found()
