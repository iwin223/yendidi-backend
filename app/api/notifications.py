from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import Announcement, DeviceRegistration, Notification, School, User, Role
from app.db.session import AsyncSession

router = APIRouter()


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    kind: str
    title: str
    body: str
    meta: Optional[Dict[str, Any]]
    read_at: Optional[datetime]
    channels: Dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class DeviceRegistrationRequest(BaseModel):
    device_token: str
    platform: str


class NotificationPreferencesRequest(BaseModel):
    push_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None
    sms_enabled: Optional[bool] = None


class AnnouncementRequest(BaseModel):
    title: str
    body: str
    audience: List[str]


@router.get("/notifications", response_model=List[NotificationResponse])
async def list_notifications(
    unread: Optional[bool] = Query(False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread:
        query = query.where(Notification.read_at.is_(None))
    result = await session.execute(query.order_by(Notification.created_at.desc()))
    return result.scalars().all()


@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Notification).where(Notification.id == notification_id, Notification.user_id == current_user.id)
    result = await session.execute(statement)
    notification = result.scalar_one_or_none()
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    notification.read_at = datetime.utcnow()
    session.add(notification)
    await session.commit()
    return {"status": "ok"}


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(Notification).where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
    result = await session.execute(query)
    for notification in result.scalars().all():
        notification.read_at = datetime.utcnow()
        session.add(notification)
    await session.commit()
    return {"status": "ok"}


@router.put("/me/devices")
async def register_device(
    request: DeviceRegistrationRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(DeviceRegistration).where(
        DeviceRegistration.user_id == current_user.id,
        DeviceRegistration.device_token == request.device_token,
    )
    result = await session.execute(stmt)
    device = result.scalar_one_or_none()
    if device:
        device.platform = request.platform
        device.updated_at = datetime.utcnow()
        session.add(device)
    else:
        device = DeviceRegistration(
            id=uuid4(),
            user_id=current_user.id,
            device_token=request.device_token,
            platform=request.platform,
        )
        session.add(device)
    await session.commit()
    return {"status": "ok", "device_token": request.device_token}


@router.patch("/me/notification-preferences")
async def update_notification_preferences(
    request: NotificationPreferencesRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    prefs = current_user.notification_preferences or {}
    prefs.update({k: v for k, v in request.dict(exclude_unset=True).items()})
    current_user.notification_preferences = prefs
    session.add(current_user)
    await session.commit()
    return {"status": "ok", "preferences": prefs}


@router.post("/schools/{school_id}/announcements")
async def create_announcement(
    school_id: UUID,
    request: AnnouncementRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.role == Role.school_admin and current_user.school_id != school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this school")
    school_stmt = select(School).where(School.id == school_id)
    school_result = await session.execute(school_stmt)
    if not school_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")
    announcement = Announcement(
        id=uuid4(),
        school_id=school_id,
        author_id=current_user.id,
        title=request.title,
        body=request.body,
        audience=request.audience,
        created_at=datetime.utcnow(),
    )
    session.add(announcement)
    await session.commit()
    return {"status": "ok", "announcement_id": str(announcement.id)}
