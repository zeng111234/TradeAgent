"""Task management API routes."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.task import Task, TaskStatus
from app.schemas.task import TaskCreate, TaskUpdate, TaskResponse

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.post("/", response_model=TaskResponse, status_code=201)
async def create_task(data: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task."""
    task = Task(**data.model_dump())
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return task


@router.get("/", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = None,
    customer_id: int | None = None,
    priority: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List tasks with optional filters."""
    query = select(Task).order_by(Task.due_date.asc().nullslast())
    if status:
        query = query.where(Task.status == status)
    if customer_id:
        query = query.where(Task.customer_id == customer_id)
    if priority:
        query = query.where(Task.priority == priority)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Get a task by ID."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(task_id: int, data: TaskUpdate, db: AsyncSession = Depends(get_db)):
    """Update a task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    # If marking as completed, set completed_at
    if data.status == "completed" and not task.completed_at:
        task.completed_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(task)
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
    await db.flush()


@router.get("/overdue/list", response_model=list[TaskResponse])
async def get_overdue_tasks(db: AsyncSession = Depends(get_db)):
    """Get all overdue tasks."""
    now = datetime.utcnow()
    result = await db.execute(
        select(Task)
        .where(Task.status == TaskStatus.PENDING, Task.due_date < now)
        .order_by(Task.due_date.asc())
    )
    return list(result.scalars().all())