from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import AuditLog, Role, User
from app.db.session import AsyncSession

router = APIRouter()


class AuditLogResponse(BaseModel):
    id: UUID
    actor_id: Optional[UUID]
    actor_name: str
    action: str
    entity_type: str
    entity_id: Optional[UUID]
    summary: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    entity_type: Optional[str] = Query(None),
    actor_id: Optional[UUID] = Query(None),
    since: Optional[datetime] = Query(None),
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may view the audit log")

    query = select(AuditLog)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if actor_id:
        query = query.where(AuditLog.actor_id == actor_id)
    if since:
        query = query.where(AuditLog.created_at >= since)
    query = query.order_by(AuditLog.created_at.desc()).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


class UserDirectoryResponse(BaseModel):
    id: UUID
    role: Role
    full_name: str
    email: Optional[str]
    phone: Optional[str]
    school_id: Optional[UUID]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/users", response_model=List[UserDirectoryResponse])
async def list_users(
    role: Optional[Role] = Query(None),
    q: Optional[str] = Query(None),
    school_id: Optional[UUID] = Query(None),
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may view the user directory")

    query = select(User)
    if role:
        query = query.where(User.role == role)
    if school_id:
        query = query.where(User.school_id == school_id)
    if q:
        query = query.where(
            User.full_name.ilike(f"%{q}%") | User.email.ilike(f"%{q}%") | User.phone.ilike(f"%{q}%")
        )
    query = query.order_by(User.created_at.desc()).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()
