"""Sender mailbox schemas.

The password is write-only on purpose: it appears in ``SmtpAccountCreate`` and
in ``SmtpAccountUpdate``, and in no response schema at all. A GET that
returned it would put the authorization code in browser devtools, in proxy
logs and in the api_surface snapshot -- three more places to leak from than
the one it is already in.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SmtpAccountCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    host: str = Field("smtp.qq.com", min_length=1, max_length=120)
    port: int = Field(465, ge=1, le=65535)
    use_tls: bool = True
    #: The From address and the login name -- the same string for QQ Mail.
    username: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=200)
    #: Switch to it immediately. Creating a second mailbox and leaving the
    #: first active is the common case; this is the "and use it now" case.
    activate: bool = False


class SmtpAccountUpdate(BaseModel):
    """Every field optional. An omitted password keeps the stored one.

    Requiring the password on every edit would mean an operator rotating a
    label has to dig out the authorization code again -- and one who cannot
    find it changes the label through the database instead of the API.
    """

    label: Optional[str] = Field(None, min_length=1, max_length=80)
    host: Optional[str] = Field(None, min_length=1, max_length=120)
    port: Optional[int] = Field(None, ge=1, le=65535)
    use_tls: Optional[bool] = None
    username: Optional[str] = Field(None, min_length=3, max_length=254)
    password: Optional[str] = Field(None, min_length=1, max_length=200)


class SmtpAccountResponse(BaseModel):
    id: int
    label: str
    host: str
    port: int
    use_tls: bool
    username: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SenderStatus(BaseModel):
    """What a send would use right now, without sending anything.

    The operator question this answers is "if I press send, which mailbox
    does it leave from, and is that mailbox real?" -- which is otherwise
    only answerable by reading .env and the table separately.
    """

    effective: str  # "account" | "env" | "simulated"
    active_account_id: Optional[int] = None
    #: True when .env carries SMTP_USER and SMTP_PASSWORD. When false and no
    #: account is active, a send is simulated and says so.
    env_configured: bool
