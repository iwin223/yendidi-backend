import csv
import io
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    Announcement,
    School,
    SchoolStatus,
    Student,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    User,
    Role,
    Wallet,
)
from app.db.session import AsyncSession

router = APIRouter()


class SchoolResponse(BaseModel):
    id: UUID
    name: str
    code: str
    region: str
    district: str
    address: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    headteacher: Optional[str]
    levels: List[str]
    status: SchoolStatus

    class Config:
        from_attributes = True


class StudentCreateRequest(BaseModel):
    student_code: str
    first_name: str
    last_name: str
    class_name: str
    level: str
    allergies: Optional[List[str]] = []
    dietary_notes: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class StudentUpdateRequest(BaseModel):
    # Partial update — every field must default to None, or Pydantic v2
    # requires it present on every PATCH regardless of `exclude_unset`.
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    class_name: Optional[str] = None
    level: Optional[str] = None
    allergies: Optional[List[str]] = None
    dietary_notes: Optional[str] = None


class SchoolCreateRequest(BaseModel):
    name: str
    code: str
    region: str
    district: str
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    headteacher: Optional[str] = None
    levels: Optional[List[str]] = []


class SchoolStatusUpdateRequest(BaseModel):
    status: SchoolStatus


class SchoolStudentResponse(BaseModel):
    id: UUID
    student_code: str
    first_name: str
    last_name: str
    class_name: str
    level: str
    allergies: List[str]
    dietary_notes: Optional[str]

    class Config:
        from_attributes = True


class SchoolEnrollmentResult(BaseModel):
    imported: int
    skipped: int
    errors: List[str]


async def _authorize_school_admin(school_id: UUID, current_user: User) -> None:
    if current_user.role == Role.super_admin:
        return
    if current_user.role != Role.school_admin or current_user.school_id != school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this school")


@router.get("/schools/{school_id}", response_model=SchoolResponse)
async def get_school(
    school_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view school details")
    statement = select(School).where(School.id == school_id)
    result = await session.execute(statement)
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")
    if current_user.role == Role.school_admin and current_user.school_id != school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this school")
    return school


@router.post("/schools", response_model=SchoolResponse, status_code=status.HTTP_201_CREATED)
async def create_school(
    request: SchoolCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may create schools")

    school = School(
        id=uuid4(),
        name=request.name,
        code=request.code,
        region=request.region,
        district=request.district,
        address=request.address,
        phone=request.phone,
        email=request.email,
        headteacher=request.headteacher,
        levels=request.levels or [],
        status=SchoolStatus.active,
    )
    session.add(school)
    subscription = Subscription(
        id=uuid4(),
        school_id=school.id,
        plan=SubscriptionPlan.trial,
        status=SubscriptionStatus.trialing,
        started_at=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=90),
        amount_minor=0,
        seats=0,
        auto_renew=False,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(school)
    return school


@router.patch("/schools/{school_id}/status")
async def update_school_status(
    school_id: UUID,
    request: SchoolStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may update school status")
    statement = select(School).where(School.id == school_id)
    result = await session.execute(statement)
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")
    school.status = request.status
    session.add(school)
    await session.commit()
    return {"status": "ok", "school_status": school.status}


@router.get("/schools/{school_id}/students", response_model=List[SchoolStudentResponse])
async def list_school_students(
    school_id: UUID,
    q: Optional[str] = Query(None),
    class_name: Optional[str] = Query(None, alias="class"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_school_admin(school_id, current_user)
    query = select(Student).where(Student.school_id == school_id)
    if q:
        query = query.where(
            (Student.first_name.ilike(f"%{q}%"))
            | (Student.last_name.ilike(f"%{q}%"))
            | (Student.student_code.ilike(f"%{q}%"))
        )
    if class_name:
        query = query.where(Student.class_name == class_name)
    result = await session.execute(query)
    return result.scalars().all()


@router.post("/schools/{school_id}/students", response_model=SchoolStudentResponse)
async def create_school_student(
    school_id: UUID,
    request: StudentCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_school_admin(school_id, current_user)
    existing_student = await session.execute(select(Student).where(Student.student_code == request.student_code))
    if existing_student.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Student code already exists")
    user = User(
        id=uuid4(),
        role=Role.student,
        full_name=f"{request.first_name} {request.last_name}",
        email=request.email,
        phone=request.phone,
        password_hash=None,
        school_id=school_id,
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(user)
    await session.flush()  # `student.user_id` FKs to this row — must exist before the student insert
    student = Student(
        id=uuid4(),
        user_id=user.id,
        school_id=school_id,
        student_code=request.student_code,
        first_name=request.first_name,
        last_name=request.last_name,
        class_name=request.class_name,
        level=request.level,
        allergies=request.allergies or [],
        dietary_notes=request.dietary_notes,
        created_at=datetime.utcnow(),
    )
    session.add(student)
    await session.flush()  # `wallet.student_id` FKs to this row — must exist before the wallet insert
    # A student is unusable without one: no wallet means no top-up and no
    # order can ever be placed for them. Every other creation path (seed.py)
    # already pairs a student with a wallet; this is the real enrollment path
    # and was silently missing it.
    session.add(Wallet(id=uuid4(), student_id=student.id))
    await session.commit()
    await session.refresh(student)
    return student


@router.post("/schools/{school_id}/students/import", response_model=SchoolEnrollmentResult)
async def import_school_students(
    school_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_school_admin(school_id, current_user)
    decoded = (await file.read()).decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded))
    imported = 0
    skipped = 0
    errors: List[str] = []
    for index, raw_row in enumerate(reader, start=1):
        # The app's own "Use sample data" template ships headers like "First
        # Name, Last Name, Class, Student ID" — human-readable, no `level`
        # column at all (the app derives level from the class name). Matching
        # only exact snake_case headers silently skipped every row of that
        # format, so normalise headers and accept the common aliases.
        row = {(k or "").strip().lower().replace(" ", "_"): v for k, v in raw_row.items()}
        student_code = row.get("student_code") or row.get("student_id")
        first_name = row.get("first_name")
        last_name = row.get("last_name")
        class_name = row.get("class_name") or row.get("class")
        level = row.get("level") or ("jhs" if class_name and "jhs" in class_name.lower() else "primary")
        if not all([student_code, first_name, last_name, class_name]):
            skipped += 1
            errors.append(f"row {index}: missing required fields")
            continue
        existing_student = await session.execute(select(Student).where(Student.student_code == student_code))
        if existing_student.scalar_one_or_none():
            skipped += 1
            errors.append(f"row {index}: student_code {student_code} already exists")
            continue
        user = User(
            id=uuid4(),
            role=Role.student,
            full_name=f"{first_name} {last_name}",
            email=None,
            phone=None,
            password_hash=None,
            school_id=school_id,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(user)
        await session.flush()  # `student.user_id` FKs to this row — must exist before the student insert
        student = Student(
            id=uuid4(),
            user_id=user.id,
            school_id=school_id,
            student_code=student_code,
            first_name=first_name,
            last_name=last_name,
            class_name=class_name,
            level=level,
            allergies=[item.strip() for item in (row.get("allergies") or "").split(",") if item.strip()],
            dietary_notes=row.get("dietary_notes") or None,
            created_at=datetime.utcnow(),
        )
        session.add(student)
        await session.flush()  # `wallet.student_id` FKs to this row — must exist before the wallet insert
        session.add(Wallet(id=uuid4(), student_id=student.id))
        imported += 1
    await session.commit()
    return SchoolEnrollmentResult(imported=imported, skipped=skipped, errors=errors)


@router.patch("/students/{student_id}", response_model=SchoolStudentResponse)
async def update_student(
    student_id: UUID,
    request: StudentUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Student).where(Student.id == student_id)
    result = await session.execute(statement)
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")
    school_id = student.school_id
    await _authorize_school_admin(school_id, current_user)
    update_data = request.dict(exclude_unset=True)
    for name, value in update_data.items():
        setattr(student, name, value)
    if request.first_name or request.last_name:
        user_stmt = select(User).where(User.id == student.user_id)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()
        if user:
            user.full_name = f"{request.first_name or student.first_name} {request.last_name or student.last_name}"
            session.add(user)
    session.add(student)
    await session.commit()
    await session.refresh(student)
    return student
