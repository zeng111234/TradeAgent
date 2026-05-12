"""Task schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class TaskBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    priority: str = "medium"
    task_type: str = "follow_up"
    due_date: Optional[datetime] = None
    customer_id: Optional[int] = None


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    task_type: Optional[str] = None
    due_date: Optional[datetime] = None
    customer_id: Optional[int] = None


class TaskResponse(TaskBase):
    id: int
    status: str
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True