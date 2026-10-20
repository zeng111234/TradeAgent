"""Email log model for tracking sent emails."""
from datetime import datetime
from sqlalchemy import Boolean, Column, Integer, String, Text, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class EmailStatus(str, enum.Enum):
    """Email sending status.

    Four of these are not about delivery:

    ``PENDING``   written, not sent -- **a proposal waiting for a human**. The
                  one state in this application where work is suspended.
    ``SENDING``   **an attempt is in flight and its outcome is unknown.** It is
                  not a claim that the mail was sent, and not a claim that it
                  was not -- it is a claim on the row, so that a second worker
                  (or a retry after a crash) cannot start a second attempt on
                  the same row. Only one state change can move a row out of
                  ``PENDING``, so the claim is atomic.
                  A row left here by a crash is resolved by ``mark_stale_sends``
                  at the next start, never left as a permanent promise.
    ``REJECTED``  a human decided against it. Kept rather than deleted, because
                  the next proposal needs to know this one was turned down, and
                  a deletion carries no information.
    ``FAILED``    the attempt was made and it did not arrive -- or, when the
                  error says so, it is not known whether it arrived.

    Adding a member is safe for an existing database: SQLite stores this as a
    plain VARCHAR with no CHECK constraint, and SQLAlchemy writes the enum
    *name*, not its value.
    """
    PENDING = "pending"        # 待发送（= 等人工确认的建议）
    SENDING = "sending"        # 已被某次发送尝试占用，结果未知
    SENT = "sent"              # 已发送
    DELIVERED = "delivered"    # 已送达
    OPENED = "opened"          # 已打开
    REPLIED = "replied"        # 已回复
    REJECTED = "rejected"      # 人工否决（保留记录）
    BOUNCED = "bounced"        # 退信
    FAILED = "failed"          # 发送失败


#: The states a send attempt can finish in. Used to decide whether a replay of
#: the same request should be answered from the record instead of sending again.
SETTLED_SEND_STATES = (
    EmailStatus.SENT,
    EmailStatus.DELIVERED,
    EmailStatus.OPENED,
    EmailStatus.REPLIED,
)


class EmailLog(Base):
    """Record of every email sent through the system."""
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Indexed: "this customer's mail" is the query the plan, the history view
    # and the debug view all ask (migration 0004).
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("email_templates.id"), nullable=True)

    # 邮件信息
    to_email = Column(String(200), nullable=False)
    to_name = Column(String(200), nullable=True)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    # Indexed: the claim scans WHERE status='pending' and the stats count by it.
    status = Column(SQLEnum(EmailStatus), default=EmailStatus.PENDING, index=True)

    # 追踪
    tracking_id = Column(String(100), nullable=True, unique=True, index=True)  # 追踪ID
    opened_at = Column(DateTime, nullable=True)  # 首次打开时间
    open_count = Column(Integer, default=0)  # 打开次数
    replied_at = Column(DateTime, nullable=True)  # 回复时间

    # 错误信息
    error_message = Column(Text, nullable=True)

    #: True when the "send" never left the process: no SMTP credentials, so the
    #: sender reported success for a message it did not hand to any server.
    #: Without this flag such a row is indistinguishable from a real send -- the
    #: status is ``sent`` either way -- and "the mail was sent but never
    #: arrived" cannot be investigated, because the first question ("was it
    #: ever really sent?") has no answer in the database. Status stays ``sent``
    #: so the follow-up flows treat it like any other outbound message; the
    #: flag is what tells a human the difference.
    simulated = Column(Boolean, default=False, nullable=False)

    # --- what was suggested before this mail existed ------------------------
    # The plan computes a kind and an attempt for every customer and used to
    # throw both away once the draft was built. A reply was therefore
    # attributable to a customer but not to the advice that produced the mail,
    # and "which suggestion gets answered" could not be asked at all.
    #
    # Indexed because "reply rate by kind" is the query the column exists for
    # and the quality report runs it every time it is opened.
    suggestion_kind = Column(String(40), nullable=True, index=True)
    #: 0 = first contact, 1 = first chase. NULL means the mail arrived without
    #: provenance: composed by hand, written by the agent's own tool, or sent
    #: before this column existed. NULL is a value, not a gap to be filled --
    #: see ``app/suggestion.py``.
    suggestion_attempt = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    sent_at = Column(DateTime, nullable=True)

    # 关联
    customer = relationship("Customer", back_populates="email_logs")

    def __repr__(self):
        return f"<EmailLog(id={self.id}, to='{self.to_email}', status='{self.status}')>"