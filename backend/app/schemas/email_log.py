"""Email log schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from app.suggestion import ACTION_KINDS, normalise


class EmailLogBase(BaseModel):
    to_email: str
    to_name: Optional[str] = None
    subject: str
    body: str


class EmailSendRequest(BaseModel):
    """Request to send a single email."""
    customer_id: Optional[int] = None
    contact_id: Optional[int] = None
    to_email: str = Field(..., description="Recipient email")
    to_name: Optional[str] = None
    template_id: Optional[int] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    # Template variables
    variables: Optional[dict] = None

    # --- provenance: which suggestion this mail came from -------------------
    # Optional, and silence is meaningful: mail composed by hand has no
    # suggestion behind it, and is reported as unattributed rather than
    # squeezed into an "other" bucket that would average into the rates.
    suggestion_kind: Optional[str] = Field(
        None,
        max_length=40,
        description=(
            "Which planned action this mail answers. One of: "
            + ", ".join(ACTION_KINDS)
        ),
    )
    suggestion_attempt: Optional[int] = Field(
        None,
        ge=0,
        le=99,
        description="0 for a first contact, 1 for the first chase, and so on",
    )

    @field_validator("suggestion_kind")
    @classmethod
    def _known_kind(cls, value: Optional[str]) -> Optional[str]:
        """Refuse a kind outside the vocabulary instead of storing it.

        An unrecognised string would become its own bucket with one row in it,
        and every rate computed from those buckets would be noise that looks
        like data. The error names the accepted values because the caller is
        usually a UI passing through what the plan gave it -- a rejection here
        is a wiring bug, and the message is what makes it findable.
        """
        cleaned = normalise(value)
        if cleaned is None:
            return None
        if cleaned not in ACTION_KINDS:
            raise ValueError(
                f"unknown suggestion_kind {value!r}; expected one of "
                + ", ".join(ACTION_KINDS)
            )
        return cleaned

    @model_validator(mode="after")
    def _attempt_needs_a_kind(self) -> "EmailSendRequest":
        # An attempt count on its own attributes a mail to nothing: "this was
        # the second try" without saying at what. Allowed the other way round,
        # because a caller can know the kind and not the count -- the by-attempt
        # table then carries an explicit unknown rather than a fabricated zero.
        if self.suggestion_attempt is not None and self.suggestion_kind is None:
            raise ValueError(
                "suggestion_attempt requires suggestion_kind: an attempt number "
                "without a kind cannot be attributed to a suggestion"
            )
        return self


class EmailBatchSendRequest(BaseModel):
    """Request to send emails to multiple recipients."""
    template_id: int
    customer_ids: list[int] = Field(..., description="List of customer IDs to send to")
    variables: Optional[dict] = None  # Additional variables


class EmailLogResponse(EmailLogBase):
    id: int
    customer_id: Optional[int] = None
    template_id: Optional[int] = None
    status: str
    tracking_id: Optional[str] = None
    opened_at: Optional[datetime] = None
    open_count: int
    replied_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime
    sent_at: Optional[datetime] = None
    #: What was suggested before this mail was written. NULL means the mail
    #: arrived without provenance, which is a fact about the record and is
    #: reported as such rather than hidden.
    suggestion_kind: Optional[str] = None
    suggestion_attempt: Optional[int] = None
    #: True when this "send" never left the process (no SMTP credentials).
    #: Status reads ``sent`` either way -- this flag is the difference between
    #: "sent and not arrived" and "never sent at all".
    simulated: bool = False
    #: True when this is a replay of a request that was already handled, and the
    #: answer came from the record instead of sending again. A caller must be able
    #: to tell a replay from a fresh delivery -- otherwise idempotency is
    #: invisible and the client cannot know whether its mail went out once.
    replayed: bool = False
    already_sent: bool = False

    class Config:
        from_attributes = True


class EmailStatsResponse(BaseModel):
    """Email statistics summary."""
    total_sent: int = 0
    total_opened: int = 0
    total_replied: int = 0
    total_bounced: int = 0
    total_failed: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0