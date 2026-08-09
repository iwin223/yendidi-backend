from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    Invoice,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    User,
    Role,
)
from app.db.session import AsyncSession

router = APIRouter()


class SubscriptionResponse(BaseModel):
    id: UUID
    school_id: UUID
    plan: SubscriptionPlan
    status: SubscriptionStatus
    started_at: datetime
    current_period_end: datetime
    amount_minor: int
    seats: int
    auto_renew: bool

    class Config:
        from_attributes = True


class InvoiceResponse(BaseModel):
    id: UUID
    subscription_id: UUID
    amount_minor: int
    status: str
    period_start: datetime
    period_end: datetime
    method: Optional[str]
    reference: str

    class Config:
        from_attributes = True


class SubscriptionChangeRequest(BaseModel):
    plan: SubscriptionPlan
    method: str


class AutoRenewRequest(BaseModel):
    auto_renew: bool


async def _authorize_school_admin(subscription: Subscription, current_user: User) -> None:
    if current_user.role == Role.super_admin:
        return
    if current_user.role != Role.school_admin or current_user.school_id != subscription.school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")


@router.get("/schools/{school_id}/subscription", response_model=SubscriptionResponse)
async def get_school_subscription(
    school_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Subscription).where(Subscription.school_id == school_id)
    result = await session.execute(statement)
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    if current_user.role not in {Role.school_admin, Role.super_admin} or (current_user.role == Role.school_admin and current_user.school_id != school_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return subscription


@router.post("/schools/{school_id}/subscription/change", response_model=SubscriptionResponse)
async def change_subscription(
    school_id: UUID,
    request: SubscriptionChangeRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Subscription).where(Subscription.school_id == school_id)
    result = await session.execute(statement)
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    if current_user.role not in {Role.school_admin, Role.super_admin} or (current_user.role == Role.school_admin and current_user.school_id != school_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    subscription.plan = request.plan
    subscription.status = SubscriptionStatus.active
    subscription.started_at = datetime.utcnow()
    subscription.current_period_end = datetime.utcnow() + timedelta(days=30 if request.plan == SubscriptionPlan.monthly else 365)
    subscription.amount_minor = 20000 if request.plan == SubscriptionPlan.monthly else 200000
    session.add(subscription)
    await session.commit()
    return subscription


@router.patch("/subscriptions/{subscription_id}/auto-renew")
async def set_auto_renew(
    subscription_id: UUID,
    request: AutoRenewRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Subscription).where(Subscription.id == subscription_id)
    result = await session.execute(statement)
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    await _authorize_school_admin(subscription, current_user)
    subscription.auto_renew = request.auto_renew
    session.add(subscription)
    await session.commit()
    return {"status": "ok", "auto_renew": subscription.auto_renew}


@router.get("/subscriptions/{subscription_id}/invoices", response_model=List[InvoiceResponse])
async def list_subscription_invoices(
    subscription_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Subscription).where(Subscription.id == subscription_id)
    result = await session.execute(statement)
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    await _authorize_school_admin(subscription, current_user)
    invoices_stmt = select(Invoice).where(Invoice.subscription_id == subscription_id).order_by(Invoice.period_start.desc())
    invoices_result = await session.execute(invoices_stmt)
    return invoices_result.scalars().all()


@router.get("/subscriptions", response_model=List[SubscriptionResponse])
async def list_subscriptions(
    status: Optional[SubscriptionStatus] = Query(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    query = select(Subscription)
    if status:
        query = query.where(Subscription.status == status)
    result = await session.execute(query)
    return result.scalars().all()
