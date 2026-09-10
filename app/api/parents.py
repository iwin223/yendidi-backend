from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import Guardianship, Parent, Role, Student, User
from app.db.session import AsyncSession

router = APIRouter()


class ParentProfileResponse(BaseModel):
    id: UUID
    user_id: UUID
    full_name: str
    phone: str
    alt_phone: Optional[str]
    occupation: Optional[str]

    class Config:
        from_attributes = True


@router.get("/parents/{parent_id}", response_model=ParentProfileResponse)
async def get_parent(
    parent_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    statement = select(Parent).where(Parent.id == parent_id)
    result = await session.execute(statement)
    parent = result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent not found")
    if current_user.role == Role.parent and parent.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this parent")
    elif current_user.role not in {Role.parent, Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this parent")
    return parent


class StudentSummary(BaseModel):
    id: UUID
    student_code: str
    first_name: str
    last_name: str
    class_name: str
    level: str

    class Config:
        from_attributes = True


async def _current_parent(parent_id: UUID, current_user: User, session: AsyncSession) -> Parent:
    statement = select(Parent).where(Parent.id == parent_id, Parent.user_id == current_user.id)
    result = await session.execute(statement)
    parent = result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parent not found or not authorized")
    return parent


@router.get("/parents/{parent_id}/students", response_model=List[StudentSummary])
async def list_parent_students(
    parent_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    parent = await _current_parent(parent_id, current_user, session)
    stmt = select(Student).join(Guardianship, Guardianship.student_id == Student.id).where(Guardianship.parent_id == parent.id)
    result = await session.execute(stmt)
    return result.scalars().all()


# `POST /parents/{id}/students` (bare student-code → instant link) is
# deliberately gone, not deprecated-in-place. A student code is a school
# prefix and four digits — about a thousand per school — printed on a card a
# child carries around all day; an endpoint that links on a bare match lets
# any parent account walk that range and become guardian to a stranger's
# child. `POST /guardian-link-requests` (guardian_links.py) replaces it with
# a request the school must approve. Dead code that reintroduces a hole if
# ever rewired is worse than no code at all — see BACKEND_HANDOVER.md §2.1a.


@router.delete("/parents/{parent_id}/students/{student_id}")
async def unlink_parent_student(
    parent_id: UUID,
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    parent = await _current_parent(parent_id, current_user, session)
    guard_stmt = select(Guardianship).where(Guardianship.parent_id == parent.id, Guardianship.student_id == student_id)
    guard_result = await session.execute(guard_stmt)
    guardianship = guard_result.scalar_one_or_none()
    if not guardianship:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guardianship not found")
    await session.delete(guardianship)
    await session.commit()
    return {"status": "ok"}
