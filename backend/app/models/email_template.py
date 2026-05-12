"""Email template model."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime

from app.database import Base


class EmailTemplate(Base):
    """Email template for marketing campaigns."""
    __tablename__ = "email_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)  # 模板名称
    subject = Column(String(500), nullable=False)  # 邮件主题，支持变量如 {company_name}
    body = Column(Text, nullable=False)  # 邮件正文，支持HTML，支持变量
    language = Column(String(20), default="en")  # en, zh, es, etc.
    category = Column(String(100), nullable=True)  # 分类：开发信/跟进/报价/节日问候
    is_ai_generated = Column(Integer, default=0)  # 是否AI生成
    use_count = Column(Integer, default=0)  # 使用次数

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<EmailTemplate(id={self.id}, name='{self.name}')>"