from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    AuditLog,
    Guardianship,
    GuardianLinkAttempt,
    GuardianLinkRequest,
    GuardianLinkStatus,
    Parent,
    Role,
    Student,
    User,
)
from app.db.session import AsyncSession

router = APIRouter()

# Far above an honest guardian mistyping a code off a card (three or four
# attempts) and far below the code space (~1,000 per school) — see
# BACKEND_HANDOVER.md §2.1a.
MAX_ATTEMPTS_PER_HOUR = 20


class GuardianLinkRequestCreate(BaseModel):
    student_code: str


class GuardianLinkRequestResponse(BaseModel):
    id: UUID
    parent_id: UUID
    parent_name: str
    parent_phone: str
    student_id: UUID
    student_name: str
    student_code: str
    school_id: UUID
    status: GuardianLinkStatus
    reviewed_by_name: Optional[str]
    reviewed_at: Optional[datetime]
    reject_reason: Optional[str]
    created_at: datetime


class GuardianLinkRejectRequest(BaseModel):
    reason: str


async def _current_parent(current_user: User, session: AsyncSession) -> Parent:
    if current_user.role != Role.parent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only parents may request a guardian link")
    result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
    parent = result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parent profile not found")
    return parent


def _to_response(req: GuardianLinkRequest, parent: Parent, parent_user: User, student: Student) -> GuardianLinkRequestResponse:
    return GuardianLinkRequestResponse(
        id=req.id,
        parent_id=parent.id,
        parent_name=parent_user.full_name,
        parent_phone=parent.phone,
        student_id=student.id,
        student_name=f"{student.first_name} {student.last_name}",
        student_code=student.student_code,
        school_id=req.school_id,
        status=req.status,
        reviewed_by_name=req.reviewed_by_name,
        reviewed_at=req.reviewed_at,
        reject_reason=req.reject_reason,
        created_at=req.created_at,
    )


@router.post("/guardian-link-requests", status_code=status.HTTP_202_ACCEPTED)
async def request_guardian_link(
    request: GuardianLinkRequestCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Always returns the same 202, whether or not the code matched anyone.

    A student code is a school prefix and four digits — about a thousand per
    school, printed on a card a child carries around. Any response that
    varies with validity turns this endpoint into an oracle for which codes
    exist, so an unknown code, a code at another school, an already-linked
    code and a freshly created request are all indistinguishable to the
    caller. See BACKEND_HANDOVER.md §2.1a.
    """
    parent = await _current_parent(current_user, session)

    # Logged before anything else so a flood of invalid codes — which create
    # no GuardianLinkRequest at all — still counts against the caller.
    since = datetime.utcnow() - timedelta(hours=1)
    attempt_count_stmt = select(GuardianLinkAttempt).where(
        GuardianLinkAttempt.parent_id == parent.id,
        GuardianLinkAttempt.created_at >= since,
    )
    attempt_result = await session.execute(attempt_count_stmt)
    if len(attempt_result.scalars().all()) >= MAX_ATTEMPTS_PER_HOUR:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Try again later.")

    session.add(GuardianLinkAttempt(id=uuid4(), parent_id=parent.id))

    generic_response = {"status": "pending", "message": "If that code matched a pupil, the school has been notified."}

    student_stmt = select(Student).where(Student.student_code == request.student_code.strip())
    student_result = await session.execute(student_stmt)
    student = student_result.scalar_one_or_none()
    if not student:
        await session.commit()
        return generic_response

    already_linked_stmt = select(Guardianship).where(
        Guardianship.parent_id == parent.id, Guardianship.student_id == student.id
    )
    already_linked_result = await session.execute(already_linked_stmt)
    if already_linked_result.scalar_one_or_none():
        await session.commit()
        return generic_response

    existing_stmt = select(GuardianLinkRequest).where(
        GuardianLinkRequest.parent_id == parent.id,
        GuardianLinkRequest.student_id == student.id,
        GuardianLinkRequest.status == GuardianLinkStatus.pending,
    )
    existing_result = await session.execute(existing_stmt)
    if existing_result.scalar_one_or_none():
        await session.commit()
        return generic_response

    session.add(
        GuardianLinkRequest(
            id=uuid4(),
            parent_id=parent.id,
            student_id=student.id,
            school_id=student.school_id,
        )
    )
    await session.commit()
    return generic_response


async def _authorize_school_admin(school_id: UUID, current_user: User) -> None:
    # The platform admin is deliberately not in this loop anywhere in this
    # feature — FAAB cannot know whose mother is whose, so its approval could
    # only be a rubber stamp. The school holds the register and decides.
    if current_user.role != Role.school_admin or current_user.school_id != school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this school")


@router.get("/schools/{school_id}/guardian-link-requests", response_model=List[GuardianLinkRequestResponse])
async def list_guardian_link_requests(
    school_id: UUID,
    link_status: Optional[GuardianLinkStatus] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_school_admin(school_id, current_user)

    query = select(GuardianLinkRequest).where(GuardianLinkRequest.school_id == school_id)
    if link_status:
        query = query.where(GuardianLinkRequest.status == link_status)
    query = query.order_by(GuardianLinkRequest.created_at.asc())
    result = await session.execute(query)
    requests = result.scalars().all()
    if not requests:
        return []

    parent_ids = {r.parent_id for r in requests}
    student_ids = {r.student_id for r in requests}
    parents = (await session.execute(select(Parent).where(Parent.id.in_(parent_ids)))).scalars().all()
    parent_by_id = {p.id: p for p in parents}
    parent_user_ids = {p.user_id for p in parents}
    parent_users = (await session.execute(select(User).where(User.id.in_(parent_user_ids)))).scalars().all()
    parent_user_by_id = {u.id: u for u in parent_users}
    students = (await session.execute(select(Student).where(Student.id.in_(student_ids)))).scalars().all()
    student_by_id = {s.id: s for s in students}

    return [
        _to_response(r, parent_by_id[r.parent_id], parent_user_by_id[parent_by_id[r.parent_id].user_id], student_by_id[r.student_id])
        for r in requests
        if r.parent_id in parent_by_id and r.student_id in student_by_id
    ]


async def _load_pending_request(session: AsyncSession, request_id: UUID) -> GuardianLinkRequest:
    result = await session.execute(select(GuardianLinkRequest).where(GuardianLinkRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guardian link request not found")
    if req.status != GuardianLinkStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That request has already been reviewed")
    return req


@router.post("/guardian-link-requests/{request_id}/approve", response_model=GuardianLinkRequestResponse)
async def approve_guardian_link_request(
    request_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    req = await _load_pending_request(session, request_id)
    await _authorize_school_admin(req.school_id, current_user)

    session.add(Guardianship(parent_id=req.parent_id, student_id=req.student_id))

    req.status = GuardianLinkStatus.approved
    req.reviewed_by_name = current_user.full_name
    req.reviewed_at = datetime.utcnow()
    session.add(req)

    parent = (await session.execute(select(Parent).where(Parent.id == req.parent_id))).scalar_one()
    parent_user = (await session.execute(select(User).where(User.id == parent.user_id))).scalar_one()
    student = (await session.execute(select(Student).where(Student.id == req.student_id))).scalar_one()

    session.add(
        AuditLog(
            id=uuid4(),
            actor_id=current_user.id,
            actor_name=current_user.full_name,
            action="guardian_link.approved",
            entity_type="guardian_link_request",
            entity_id=req.id,
            summary=f"Linked {parent_user.full_name} to {student.first_name} {student.last_name}",
        )
    )

    await session.commit()
    await session.refresh(req)
    return _to_response(req, parent, parent_user, student)


@router.post("/guardian-link-requests/{request_id}/reject", response_model=GuardianLinkRequestResponse)
async def reject_guardian_link_request(
    request_id: UUID,
    request: GuardianLinkRejectRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not request.reason.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required")

    req = await _load_pending_request(session, request_id)
    await _authorize_school_admin(req.school_id, current_user)

    req.status = GuardianLinkStatus.rejected
    req.reviewed_by_name = current_user.full_name
    req.reviewed_at = datetime.utcnow()
    req.reject_reason = request.reason.strip()
    session.add(req)

    parent = (await session.execute(select(Parent).where(Parent.id == req.parent_id))).scalar_one()
    parent_user = (await session.execute(select(User).where(User.id == parent.user_id))).scalar_one()
    student = (await session.execute(select(Student).where(Student.id == req.student_id))).scalar_one()

    session.add(
        AuditLog(
            id=uuid4(),
            actor_id=current_user.id,
            actor_name=current_user.full_name,
            action="guardian_link.rejected",
            entity_type="guardian_link_request",
            entity_id=req.id,
            summary=f"Declined {parent_user.full_name}'s claim on {student.first_name} {student.last_name}: {request.reason.strip()}",
        )
    )

    await session.commit()
    await session.refresh(req)
    return _to_response(req, parent, parent_user, student)
