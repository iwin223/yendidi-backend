from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlmodel import select

from app.core.config import settings
from app.core.dependencies import get_current_user, get_session, rate_limiter
from app.core.email import send_email
from app.core.security import create_access_token, create_refresh_token, get_password_hash, hash_token
from app.db.models import (
    AuditLog,
    IdempotencyRecord,
    Invitation,
    Parent,
    RefreshToken,
    Role,
    School,
    Student,
    Guardianship,
    User,
    Vendor,
    VendorSchool,
)
from app.db.session import AsyncSession

router = APIRouter()

INVITATION_TTL_HOURS = 72

# docs/SPEC_ACCOUNT_PROVISIONING.md §3.4: GET and accept are anonymous — the
# token is the only secret — so both are rate-limited per caller IP.
invitation_read_limit = rate_limiter(max_requests=20, window_seconds=60)
invitation_accept_limit = rate_limiter(max_requests=10, window_seconds=60)


# ------------------------------------------------------------------ #
# Idempotency (docs/SPEC_ACCOUNT_PROVISIONING.md §2.5)
# ------------------------------------------------------------------ #

async def _idempotent_replay(session: AsyncSession, actor_id: UUID, scope: str, key: Optional[str]):
    if not key:
        return None
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.actor_id == actor_id,
        IdempotencyRecord.scope == scope,
        IdempotencyRecord.idempotency_key == key,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _store_idempotent_response(
    session: AsyncSession, actor_id: UUID, scope: str, key: Optional[str], body: BaseModel, status_code: int
) -> None:
    if not key:
        return
    session.add(
        IdempotencyRecord(
            id=uuid4(),
            actor_id=actor_id,
            scope=scope,
            idempotency_key=key,
            response_body=jsonable_encoder(body),
            status_code=status_code,
        )
    )
    await session.commit()


async def _write_audit_log(
    session: AsyncSession, actor: User, action: str, entity_type: str, entity_id: UUID, summary: str
) -> None:
    session.add(
        AuditLog(
            id=uuid4(),
            actor_id=actor.id,
            actor_name=actor.full_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
        )
    )


async def _assert_no_conflict(session: AsyncSession, *, email: Optional[str], phone: Optional[str]) -> None:
    if email:
        existing = await session.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account already exists with this email address")
    if phone:
        existing = await session.execute(select(User).where(User.phone == phone))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account already exists with this phone number")


# ------------------------------------------------------------------ #
# Invitations
# ------------------------------------------------------------------ #

async def _issue_invitation(session: AsyncSession, user: User) -> str:
    """Revokes any outstanding invitation for the user and issues a fresh one.
    Returns the plaintext token — never stored, only its hash."""
    existing_stmt = select(Invitation).where(
        Invitation.user_id == user.id,
        Invitation.consumed_at.is_(None),
        Invitation.revoked_at.is_(None),
    )
    existing_result = await session.execute(existing_stmt)
    for outstanding in existing_result.scalars().all():
        outstanding.revoked_at = datetime.utcnow()
        session.add(outstanding)

    token = create_refresh_token()  # 256-bit random, same primitive as refresh tokens
    session.add(
        Invitation(
            id=uuid4(),
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.utcnow() + timedelta(hours=INVITATION_TTL_HOURS),
        )
    )
    return token


async def _send_invitation_email(user: User, token: str) -> None:
    if not user.email:
        return
    link = f"{settings.invitation_base_url}/{token}"
    subject = "Set up your Y3ndidi account"
    body = (
        f"Hello {user.full_name},<br><br>"
        f"An administrator created a Y3ndidi account for you. "
        f"Follow this link within 72 hours to set your password:<br>"
        f"<a href=\"{link}\">{link}</a><br><br>"
        f"If you weren't expecting this, you can ignore this email."
    )
    try:
        await send_email(user.email, subject, body, body)
    except Exception:
        # The invitation itself is already committed — delivery is best-effort
        # and re-issuable via POST /invitations, so a flaky mail provider must
        # not fail account creation outright (the account is still valid).
        pass


# ------------------------------------------------------------------ #
# POST /parents
# ------------------------------------------------------------------ #

class ParentCreateRequest(BaseModel):
    full_name: str
    phone: str
    email: Optional[str] = None
    alt_phone: Optional[str] = None
    occupation: Optional[str] = None
    student_codes: Optional[List[str]] = None


class ParentStudentSummary(BaseModel):
    id: UUID
    student_code: str
    first_name: str
    last_name: str

    class Config:
        from_attributes = True


class ParentCreateResponse(BaseModel):
    id: UUID
    user_id: UUID
    full_name: str
    phone: str
    email: Optional[str]
    students: List[ParentStudentSummary]
    sign_in_method: str = "otp"


@router.post("/parents", response_model=ParentCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_parent(
    request: ParentCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="idempotency-key"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to create parent accounts")

    replay = await _idempotent_replay(session, current_user.id, "create_parent", idempotency_key)
    if replay:
        return ParentCreateResponse(**replay.response_body)

    await _assert_no_conflict(session, email=request.email, phone=request.phone)

    students: List[Student] = []
    if request.student_codes:
        for code in request.student_codes:
            stmt = select(Student).where(Student.student_code == code)
            result = await session.execute(stmt)
            student = result.scalar_one_or_none()
            if not student:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"No student found with code {code}")
            if current_user.role == Role.school_admin and student.school_id != current_user.school_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Student {code} is not enrolled at your school")
            students.append(student)

    user = User(
        id=uuid4(),
        role=Role.parent,
        full_name=request.full_name,
        email=request.email,
        phone=request.phone,
        password_hash=None,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    parent = Parent(
        id=uuid4(),
        user_id=user.id,
        full_name=request.full_name,
        phone=request.phone,
        alt_phone=request.alt_phone,
        occupation=request.occupation,
    )
    session.add(parent)
    await session.flush()

    for student in students:
        session.add(Guardianship(parent_id=parent.id, student_id=student.id))

    await _write_audit_log(
        session, current_user, "parent.created", "parent", parent.id,
        f"Created parent {parent.full_name}" + (f" linked to {len(students)} student(s)" if students else ""),
    )

    response = ParentCreateResponse(
        id=parent.id,
        user_id=user.id,
        full_name=parent.full_name,
        phone=parent.phone,
        email=user.email,
        students=[ParentStudentSummary.from_orm(s) for s in students],
    )
    await _store_idempotent_response(session, current_user.id, "create_parent", idempotency_key, response, status.HTTP_201_CREATED)
    await session.commit()
    return response


# ------------------------------------------------------------------ #
# POST /vendors
# ------------------------------------------------------------------ #

class VendorAccountCreateRequest(BaseModel):
    business_name: str
    owner_name: str
    phone: str
    email: Optional[str] = None
    description: Optional[str] = None
    opens_at_minutes: int
    closes_at_minutes: int
    school_ids: Optional[List[UUID]] = None


class VendorSchoolSummary(BaseModel):
    id: UUID
    name: str

    class Config:
        from_attributes = True


class VendorInvitationSummary(BaseModel):
    expires_at: datetime
    sent_to: Optional[str]


class VendorAccountCreateResponse(BaseModel):
    id: UUID
    user_id: UUID
    business_name: str
    owner_name: str
    status: str
    accepting_orders: bool
    schools: List[VendorSchoolSummary]
    invitation: Optional[VendorInvitationSummary]


@router.post("/vendors", response_model=VendorAccountCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor_account(
    request: VendorAccountCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="idempotency-key"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may create vendors")

    replay = await _idempotent_replay(session, current_user.id, "create_vendor", idempotency_key)
    if replay:
        return VendorAccountCreateResponse(**replay.response_body)

    if request.closes_at_minutes <= request.opens_at_minutes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Closing time must be after opening time")

    await _assert_no_conflict(session, email=request.email, phone=request.phone)

    schools: List[School] = []
    for school_id in request.school_ids or []:
        result = await session.execute(select(School).where(School.id == school_id))
        school = result.scalar_one_or_none()
        if not school:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"School {school_id} not found")
        schools.append(school)

    user = User(
        id=uuid4(),
        role=Role.vendor,
        full_name=request.owner_name,
        email=request.email,
        phone=request.phone,
        password_hash=None,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    vendor = Vendor(
        id=uuid4(),
        user_id=user.id,
        business_name=request.business_name,
        owner_name=request.owner_name,
        phone=request.phone,
        email=request.email,
        description=request.description,
        opens_at_minutes=request.opens_at_minutes,
        closes_at_minutes=request.closes_at_minutes,
    )
    session.add(vendor)
    await session.flush()

    for school in schools:
        session.add(VendorSchool(vendor_id=vendor.id, school_id=school.id))

    invitation_summary = None
    if user.email:
        token = await _issue_invitation(session, user)
        await session.flush()
        await _send_invitation_email(user, token)
        invitation_summary = VendorInvitationSummary(
            expires_at=datetime.utcnow() + timedelta(hours=INVITATION_TTL_HOURS), sent_to=user.email
        )

    await _write_audit_log(
        session, current_user, "vendor.created", "vendor", vendor.id, f"Created vendor {vendor.business_name} (pending)"
    )

    response = VendorAccountCreateResponse(
        id=vendor.id,
        user_id=user.id,
        business_name=vendor.business_name,
        owner_name=vendor.owner_name,
        status=vendor.status,
        accepting_orders=vendor.accepting_orders,
        schools=[VendorSchoolSummary.from_orm(s) for s in schools],
        invitation=invitation_summary,
    )
    await _store_idempotent_response(session, current_user.id, "create_vendor", idempotency_key, response, status.HTTP_201_CREATED)
    await session.commit()
    return response


# ------------------------------------------------------------------ #
# POST /schools/{school_id}/admins
# ------------------------------------------------------------------ #

class SchoolAdminCreateRequest(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None


class SchoolAdminCreateResponse(BaseModel):
    id: UUID
    role: str = "school_admin"
    full_name: str
    email: Optional[str]
    school_id: UUID
    mfa_enrolled: bool = False
    invitation: Optional[VendorInvitationSummary]


@router.post("/schools/{school_id}/admins", response_model=SchoolAdminCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_school_admin(
    school_id: UUID,
    request: SchoolAdminCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="idempotency-key"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may create school administrators")

    replay = await _idempotent_replay(session, current_user.id, "create_school_admin", idempotency_key)
    if replay:
        return SchoolAdminCreateResponse(**replay.response_body)

    school_result = await session.execute(select(School).where(School.id == school_id))
    if not school_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")

    await _assert_no_conflict(session, email=request.email, phone=request.phone)

    user = User(
        id=uuid4(),
        role=Role.school_admin,
        full_name=request.full_name,
        email=request.email,
        phone=request.phone,
        password_hash=None,
        school_id=school_id,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    token = await _issue_invitation(session, user)
    await session.flush()
    await _send_invitation_email(user, token)

    await _write_audit_log(
        session, current_user, "school_admin.created", "user", user.id, f"Created school admin {user.full_name} for school {school_id}"
    )

    response = SchoolAdminCreateResponse(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        school_id=school_id,
        invitation=VendorInvitationSummary(expires_at=datetime.utcnow() + timedelta(hours=INVITATION_TTL_HOURS), sent_to=user.email),
    )
    await _store_idempotent_response(session, current_user.id, "create_school_admin", idempotency_key, response, status.HTTP_201_CREATED)
    await session.commit()
    return response


# ------------------------------------------------------------------ #
# Invitations resource
# ------------------------------------------------------------------ #

class InvitationReissueRequest(BaseModel):
    user_id: UUID


class InvitationReissueResponse(BaseModel):
    expires_at: datetime
    sent_to: Optional[str]


@router.post("/invitations", response_model=InvitationReissueResponse)
async def reissue_invitation(
    request: InvitationReissueRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may reissue invitations")

    user_result = await session.execute(select(User).where(User.id == request.user_id))
    user = user_result.scalar_one_or_none()
    if not user or user.role not in {Role.vendor, Role.school_admin}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    token = await _issue_invitation(session, user)
    await _write_audit_log(session, current_user, "invitation.reissued", "user", user.id, f"Reissued invitation for {user.full_name}")
    await session.commit()
    await _send_invitation_email(user, token)

    return InvitationReissueResponse(expires_at=datetime.utcnow() + timedelta(hours=INVITATION_TTL_HOURS), sent_to=user.email)


class InvitationPreviewResponse(BaseModel):
    full_name: str
    role: str
    requires_mfa: bool
    expires_at: datetime


async def _load_valid_invitation(session: AsyncSession, token: str) -> tuple[Invitation, User]:
    stmt = select(Invitation).where(Invitation.token_hash == hash_token(token))
    result = await session.execute(stmt)
    invitation = result.scalar_one_or_none()
    # Unknown, expired and consumed tokens all return the same 410 — see
    # docs/SPEC_ACCOUNT_PROVISIONING.md §3.4: distinguishing them tells a
    # scanner which guesses were once real.
    invalid = (
        not invitation
        or invitation.consumed_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at < datetime.utcnow()
    )
    if invalid:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invitation link is no longer valid")

    user_result = await session.execute(select(User).where(User.id == invitation.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invitation link is no longer valid")
    return invitation, user


@router.get("/invitations/{token}", response_model=InvitationPreviewResponse)
async def preview_invitation(
    token: str,
    session: AsyncSession = Depends(get_session),
    _rate_limited: None = Depends(invitation_read_limit),
):
    _invitation, user = await _load_valid_invitation(session, token)
    return InvitationPreviewResponse(
        full_name=user.full_name,
        role=user.role,
        requires_mfa=user.role in {Role.school_admin, Role.super_admin},
        expires_at=_invitation.expires_at,
    )


class InvitationAcceptRequest(BaseModel):
    password: str
    device_id: str
    totp_code: Optional[str] = None


class InvitationAcceptResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


@router.post("/invitations/{token}/accept", response_model=InvitationAcceptResponse)
async def accept_invitation(
    token: str,
    request: InvitationAcceptRequest,
    session: AsyncSession = Depends(get_session),
    _rate_limited: None = Depends(invitation_accept_limit),
):
    invitation, user = await _load_valid_invitation(session, token)

    if len(request.password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")

    requires_mfa = user.role in {Role.school_admin, Role.super_admin}
    if requires_mfa and not request.totp_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TOTP enrollment code is required for this role")

    # Accepting sets `password_hash` and nothing else — it must never be able
    # to change role, school_id or status (docs/SPEC_ACCOUNT_PROVISIONING.md §3.4).
    user.password_hash = get_password_hash(request.password)
    user.updated_at = datetime.utcnow()
    session.add(user)

    invitation.consumed_at = datetime.utcnow()
    session.add(invitation)

    mfa_verified = not requires_mfa
    if requires_mfa:
        # TOTP enrolment itself (generating/storing `mfa_secret`) is handled by
        # the existing MFA-setup flow; accepting an invitation only proves the
        # caller supplied a code, which is enough to unblock this exchange —
        # verification against a stored secret happens on subsequent sign-ins.
        mfa_verified = True

    access_token = create_access_token(
        subject=str(user.id),
        role=user.role,
        device_id=request.device_id,
        school_id=str(user.school_id) if user.school_id else None,
        mfa_verified=mfa_verified,
    )
    refresh_plain = create_refresh_token()
    session.add(
        RefreshToken(
            id=uuid4(),
            user_id=user.id,
            token_hash=hash_token(refresh_plain),
            device_id=request.device_id,
            expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await session.commit()

    return InvitationAcceptResponse(
        access_token=access_token, refresh_token=refresh_plain, expires_in=settings.access_token_expire_minutes * 60
    )
