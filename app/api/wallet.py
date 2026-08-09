from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    Guardianship,
    Parent,
    Role,
    Student,
    Topup,
    TopupStatus,
    User,
    Wallet,
    WalletTransaction,
)
from app.db.session import AsyncSession
from app.payments import create_paystack_transaction

router = APIRouter()


class WalletTopupRequest(BaseModel):
    amount_minor: int
    method: str
    payer_reference: str


class TopupResponse(BaseModel):
    topup_id: UUID
    status: str
    message: str
    paystack_url: Optional[str]


class WalletResponse(BaseModel):
    id: UUID
    student_id: UUID
    balance_minor: int
    frozen: bool
    daily_limit_minor: Optional[int]
    weekly_limit_minor: Optional[int]
    blocked_categories: List[str]
    low_balance_threshold_minor: int
    updated_at: datetime

    class Config:
        from_attributes = True


class WalletTransactionResponse(BaseModel):
    id: UUID
    wallet_id: UUID
    student_id: UUID
    type: str
    amount_minor: int
    balance_after_minor: int
    description: str
    method: Optional[str]
    reference: str
    order_id: Optional[UUID]
    actor_id: Optional[UUID]
    created_at: datetime

    class Config:
        from_attributes = True


class TopupStatusResponse(BaseModel):
    id: UUID
    wallet_id: UUID
    status: TopupStatus
    amount_minor: int
    method: str
    processor: str
    processor_ref: Optional[str]
    authorization_url: Optional[str]
    failure_reason: Optional[str]
    created_at: datetime
    settled_at: Optional[datetime]

    class Config:
        from_attributes = True


class WalletControlUpdate(BaseModel):
    daily_limit_minor: Optional[int]
    weekly_limit_minor: Optional[int]
    blocked_categories: Optional[List[str]]
    low_balance_threshold_minor: Optional[int]


class WalletFreezeRequest(BaseModel):
    frozen: bool


async def ensure_wallet_access(wallet: Wallet, current_user: User, session: AsyncSession) -> None:
    if current_user.role == "student":
        student_stmt = select(Student).where(Student.user_id == current_user.id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.id != wallet.student_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Wallet does not belong to the current student")
        return

    if current_user.role == "parent":
        parent_stmt = select(Parent).where(Parent.user_id == current_user.id)
        parent_result = await session.execute(parent_stmt)
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parent profile not found")
        guard_stmt = select(Guardianship).where(
            Guardianship.parent_id == parent.id,
            Guardianship.student_id == wallet.student_id,
        )
        guard_result = await session.execute(guard_stmt)
        if not guard_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parent does not have access to this wallet")
        return

    if current_user.role == Role.school_admin:
        student_stmt = select(Student).where(Student.id == wallet.student_id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.school_id != current_user.school_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this wallet")
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this wallet")


@router.post("/wallets/{wallet_id}/topups", response_model=TopupResponse)
async def create_topup(
    wallet_id: UUID,
    request: WalletTopupRequest,
    idempotency_key: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header required")
    statement = select(Wallet).where(Wallet.id == wallet_id)
    result = await session.execute(statement)
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

    if current_user.role != Role.parent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only parents may top up wallets")

    await ensure_wallet_access(wallet, current_user, session)

    if request.amount_minor <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be greater than zero")

    existing_stmt = select(Topup).where(
        Topup.wallet_id == wallet_id,
        Topup.initiated_by == current_user.id,
        Topup.idempotency_key == idempotency_key,
    )
    existing_result = await session.execute(existing_stmt)
    existing_topup = existing_result.scalar_one_or_none()
    if existing_topup:
        return TopupResponse(
            topup_id=existing_topup.id,
            status=existing_topup.status,
            message="Existing topup returned",
            paystack_url=existing_topup.authorization_url,
        )

    topup = Topup(
        wallet_id=wallet_id,
        initiated_by=current_user.id,
        amount_minor=request.amount_minor,
        method=request.method,
        processor="paystack",
        idempotency_key=idempotency_key,
        status=TopupStatus.pending,
        created_at=datetime.utcnow(),
    )
    session.add(topup)
    await session.commit()
    await session.refresh(topup)

    transaction = await create_paystack_transaction(
        amount_minor=request.amount_minor,
        payer_reference=request.payer_reference,
        topup_id=str(topup.id),
    )
    topup.authorization_url = transaction.get("authorization_url")
    topup.processor_data = transaction
    session.add(topup)
    await session.commit()

    return TopupResponse(
        topup_id=topup.id,
        status=topup.status,
        message="Payment initiated",
        paystack_url=topup.authorization_url,
    )


@router.get("/students/{student_id}/wallet", response_model=WalletResponse)
async def get_student_wallet(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Wallet).where(Wallet.student_id == student_id)
    result = await session.execute(statement)
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    await ensure_wallet_access(wallet, current_user, session)
    if current_user.role == Role.school_admin:
        student_stmt = select(Student).where(Student.id == wallet.student_id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.school_id != current_user.school_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return wallet


@router.get("/wallets/{wallet_id}/transactions", response_model=List[WalletTransactionResponse])
async def get_wallet_transactions(
    wallet_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    wallet_stmt = select(Wallet).where(Wallet.id == wallet_id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    await ensure_wallet_access(wallet, current_user, session)

    transaction_stmt = select(WalletTransaction).where(WalletTransaction.wallet_id == wallet_id).order_by(WalletTransaction.created_at.desc())
    transaction_result = await session.execute(transaction_stmt)
    return transaction_result.scalars().all()


@router.get("/topups/{topup_id}", response_model=TopupStatusResponse)
async def get_topup_status(
    topup_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    topup_stmt = select(Topup).where(Topup.id == topup_id)
    topup_result = await session.execute(topup_stmt)
    topup = topup_result.scalar_one_or_none()
    if not topup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topup not found")

    wallet_stmt = select(Wallet).where(Wallet.id == topup.wallet_id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    await ensure_wallet_access(wallet, current_user, session)
    return topup


@router.patch("/wallets/{wallet_id}/controls")
async def update_wallet_controls(
    wallet_id: UUID,
    request: WalletControlUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    wallet_stmt = select(Wallet).where(Wallet.id == wallet_id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    if current_user.role != "parent":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only parents may update wallet controls")
    await ensure_wallet_access(wallet, current_user, session)

    update_data = request.dict(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(wallet, field_name, value)
    session.add(wallet)
    await session.commit()
    return {"status": "ok"}


@router.patch("/wallets/{wallet_id}/freeze")
async def freeze_wallet(
    wallet_id: UUID,
    request: WalletFreezeRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    wallet_stmt = select(Wallet).where(Wallet.id == wallet_id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    if current_user.role != "parent":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only parents may freeze wallets")
    await ensure_wallet_access(wallet, current_user, session)

    wallet.frozen = request.frozen
    session.add(wallet)
    await session.commit()
    return {"status": "ok", "frozen": wallet.frozen}
