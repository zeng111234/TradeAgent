"""Email log model for tracking sent emails."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class EmailStatus(str, enum.Enum):
    """Email sending status."""
    PENDING = "pending"        # 待发送
    SENT = "sent"              # 已发送
    DELIVERED = "delivered"    # 已送达
    OPENED = "opened"          # 已打开
    REPLIED = "replied"        # 已回复
    BOUNCED = "bounced"        # 退信
    FAILED = "failed"          # 发送失败


class EmailLog(Base):
    """Record of every email sent through the system."""
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("email_templates.id"), nullable=True)

    # 邮件信息
    to_email = Column(String(200), nullable=False)
    to_name = Column(String(200), nullable=True)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(SQLEnum(EmailStatus), default=EmailStatus.PENDING)

    # 追踪
    tracking_id = Column(String(100), nullable=True, unique=True, index=True)  # 追踪ID
    opened_at = Column(DateTime, nullable=True)  # 首次打开时间
    open_count = Column(Integer, default=0)  # 打开次数
    replied_at = Column(DateTime, nullable=True)  # 回复时间

    # 错误信息
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

    # 关联
    customer = relationship("Customer", back_populates="email_logs")

    def __repr__(self):
        return f"<EmailLog(id={self.id}, to='{self.to_email}', status='{self.status}')>"