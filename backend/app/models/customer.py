"""Customer related database models."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class CustomerStage(str, enum.Enum):
    """Customer pipeline stage."""
    NEW = "new"                # 新客户
    CONTACTED = "contacted"    # 已联系
    INTERESTED = "interested"  # 有意向
    QUOTING = "quoting"        # 报价中
    SAMPLE = "sample"          # 寄样中
    ORDERING = "ordering"      # 下单中
    COMPLETED = "completed"    # 已成交
    LOST = "lost"              # 已流失


class CustomerSource(str, enum.Enum):
    """Customer source channel."""
    MANUAL = "manual"          # 手动录入
    IMPORT = "import"          # 批量导入
    SCRAPER = "scraper"        # 数据采集
    REFERRAL = "referral"      # 客户推荐
    EXHIBITION = "exhibition"  # 展会
    ALIBABA = "alibaba"        # 阿里国际站
    OTHER = "other"            # 其他


class Customer(Base):
    """Customer model - core entity."""
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_name = Column(String(200), nullable=False, index=True)
    company_name_cn = Column(String(200), nullable=True)  # 中文公司名
    country = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)  # 省/州
    city = Column(String(100), nullable=True)
    website = Column(String(500), nullable=True)
    industry = Column(String(200), nullable=True)  # 行业
    products = Column(Text, nullable=True)  # 采购产品关键词
    source = Column(SQLEnum(CustomerSource), default=CustomerSource.MANUAL)
    stage = Column(SQLEnum(CustomerStage), default=CustomerStage.NEW)
    score = Column(Float, default=0.0)  # AI匹配评分 0-100
    tags = Column(String(500), nullable=True)  # 逗号分隔的标签
    annual_import_value = Column(String(100), nullable=True)  # 年进口额
    notes = Column(Text, nullable=True)

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_contacted_at = Column(DateTime, nullable=True)
    next_follow_up_at = Column(DateTime, nullable=True)

    # 关联
    contacts = relationship("CustomerContact", back_populates="customer", cascade="all, delete-orphan")
    notes_list = relationship("CustomerNote", back_populates="customer", cascade="all, delete-orphan")
    email_logs = relationship("EmailLog", back_populates="customer", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Customer(id={self.id}, company='{self.company_name}', stage='{self.stage}')>"


class CustomerContact(Base):
    """Customer contact person model."""
    __tablename__ = "customer_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    name = Column(String(100), nullable=False)
    title = Column(String(100), nullable=True)  # 职位
    email = Column(String(200), nullable=True)
    phone = Column(String(50), nullable=True)
    whatsapp = Column(String(50), nullable=True)
    linkedin = Column(String(300), nullable=True)
    is_primary = Column(Integer, default=0)  # 是否主要联系人
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    customer = relationship("Customer", back_populates="contacts")

    def __repr__(self):
        return f"<CustomerContact(id={self.id}, name='{self.name}', email='{self.email}')>"


class CustomerNote(Base):
    """Customer interaction notes / communication timeline."""
    __tablename__ = "customer_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    content = Column(Text, nullable=False)
    note_type = Column(String(50), default="general")  # general, email, call, meeting, quotation

    created_at = Column(DateTime, default=datetime.utcnow)

    # 关联
    customer = relationship("Customer", back_populates="notes_list")

    def __repr__(self):
        return f"<CustomerNote(id={self.id}, type='{self.note_type}')>"