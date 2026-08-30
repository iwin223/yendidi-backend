from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    AuditLog,
    Role,
    User,
    Vendor,
    VendorSchool,
    VendorSubmission,
    VendorSubmissionStatus,
)
from app.db.session import AsyncSession

router = APIRouter()


class VendorSubmissionCreateRequest(BaseModel):
    business_name: str
    owner_name: str
    phone: str
    email: Optional[str] = None
    description: Optional[str] = None
    opens_at_minutes: int
    closes_at_minutes: int


class VendorSubmissionResponse(BaseModel):
    id: UUID
    school_id: UUID
    submitted_by_user_id: UUID
    submitted_by_name: str
    business_name: str
    owner_name: str
    phone: str
    email: Optional[str]
    description: Optional[str]
    opens_at_minutes: int
    closes_at_minutes: int
    status: VendorSubmissionStatus
    submitted_at: datetime
    reviewed_by_name: Optional[str]
    reviewed_at: Optional[datetime]
    review_note: Optional[str]
    vendor_id: Optional[UUID]

    class Config:
        from_attributes = True


class VendorSubmissionRejectRequest(BaseModel):
    reason: str


async def _write_audit_log(session: AsyncSession, actor: User, action: str, entity_id: UUID, summary: str) -> None:
    session.add(
        AuditLog(
            id=uuid4(),
            actor_id=actor.id,
            actor_name=actor.full_name,
            action=action,
            entity_type="vendor_submission",
            entity_id=entity_id,
            summary=summary,
        )
    )


@router.post("/vendor-submissions", response_model=VendorSubmissionResponse, status_code=status.HTTP_201_CREATED)
async def submit_vendor_for_review(
    request: VendorSubmissionCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.school_admin or not current_user.school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only school administrators may submit a vendor for review")

    if request.closes_at_minutes <= request.opens_at_minutes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Closing time must be after opening time")

    duplicate_stmt = select(VendorSubmission).where(
        VendorSubmission.school_id == current_user.school_id,
        VendorSubmission.status == VendorSubmissionStatus.pending,
        VendorSubmission.business_name.ilike(request.business_name.strip()),
    )
    duplicate_result = await session.execute(duplicate_stmt)
    if duplicate_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That vendor is already awaiting review")

    submission = VendorSubmission(
        id=uuid4(),
        school_id=current_user.school_id,
        submitted_by_user_id=current_user.id,
        submitted_by_name=current_user.full_name,
        business_name=request.business_name.strip(),
        owner_name=request.owner_name.strip(),
        phone=request.phone.strip(),
        email=request.email,
        description=request.description,
        opens_at_minutes=request.opens_at_minutes,
        closes_at_minutes=request.closes_at_minutes,
    )
    session.add(submission)
    await session.commit()
    await session.refresh(submission)
    return submission


@router.get("/vendor-submissions", response_model=List[VendorSubmissionResponse])
async def list_vendor_submissions(
    submission_status: Optional[VendorSubmissionStatus] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may review vendor submissions")

    query = select(VendorSubmission)
    if submission_status:
        query = query.where(VendorSubmission.status == submission_status)
    # Oldest first while pending: the school that waited longest is next.
    if submission_status == VendorSubmissionStatus.pending or submission_status is None:
        query = query.order_by(VendorSubmission.submitted_at.asc())
    else:
        query = query.order_by(VendorSubmission.submitted_at.desc())
    result = await session.execute(query)
    return result.scalars().all()


@router.get("/schools/{school_id}/vendor-submissions", response_model=List[VendorSubmissionResponse])
async def list_school_vendor_submissions(
    school_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == Role.school_admin:
        if current_user.school_id != school_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this school")
    elif current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    query = (
        select(VendorSubmission)
        .where(VendorSubmission.school_id == school_id)
        .order_by(VendorSubmission.submitted_at.desc())
    )
    result = await session.execute(query)
    return result.scalars().all()


async def _load_pending_submission(session: AsyncSession, submission_id: UUID) -> VendorSubmission:
    result = await session.execute(select(VendorSubmission).where(VendorSubmission.id == submission_id))
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if submission.status != VendorSubmissionStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That submission has already been reviewed")
    return submission


@router.post("/vendor-submissions/{submission_id}/approve", response_model=VendorSubmissionResponse)
async def approve_vendor_submission(
    submission_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may approve vendor submissions")

    submission = await _load_pending_submission(session, submission_id)

    # Approval creates the vendor in `pending` — the same state `POST /vendors`
    # would produce. It still needs food-safety documents verified before it
    # can sell (docs/SPEC_ACCOUNT_PROVISIONING.md §2.2).
    user = User(
        id=uuid4(),
        role=Role.vendor,
        full_name=submission.owner_name,
        email=submission.email,
        phone=submission.phone,
        password_hash=None,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    vendor = Vendor(
        id=uuid4(),
        user_id=user.id,
        business_name=submission.business_name,
        owner_name=submission.owner_name,
        phone=submission.phone,
        email=submission.email,
        description=submission.description,
        opens_at_minutes=submission.opens_at_minutes,
        closes_at_minutes=submission.closes_at_minutes,
    )
    session.add(vendor)
    await session.flush()
    session.add(VendorSchool(vendor_id=vendor.id, school_id=submission.school_id))

    submission.status = VendorSubmissionStatus.approved
    submission.reviewed_by_name = current_user.full_name
    submission.reviewed_at = datetime.utcnow()
    submission.vendor_id = vendor.id
    session.add(submission)

    await _write_audit_log(
        session, current_user, "vendor_submission.approved", submission.id,
        f"Approved {submission.business_name} proposed by {submission.submitted_by_name}",
    )

    await session.commit()
    await session.refresh(submission)
    return submission


@router.post("/vendor-submissions/{submission_id}/reject", response_model=VendorSubmissionResponse)
async def reject_vendor_submission(
    submission_id: UUID,
    request: VendorSubmissionRejectRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may reject vendor submissions")
    if not request.reason.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required so the school knows what to fix")

    submission = await _load_pending_submission(session, submission_id)

    submission.status = VendorSubmissionStatus.rejected
    submission.reviewed_by_name = current_user.full_name
    submission.reviewed_at = datetime.utcnow()
    submission.review_note = request.reason.strip()
    session.add(submission)

    await _write_audit_log(
        session, current_user, "vendor_submission.rejected", submission.id,
        f"Declined {submission.business_name}: {request.reason.strip()}",
    )

    await session.commit()
    await session.refresh(submission)
    return submission
