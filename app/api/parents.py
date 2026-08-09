from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import Guardianship, Parent, Student, User
from app.db.session import AsyncSession

router = APIRouter()


class ParentStudentLinkRequest(BaseModel):
    student_code: str


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


@router.post("/parents/{parent_id}/students", response_model=StudentSummary)
async def link_parent_student(
    parent_id: UUID,
    request: ParentStudentLinkRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    parent = await _current_parent(parent_id, current_user, session)
    student_stmt = select(Student).where(Student.student_code == request.student_code)
    student_result = await session.execute(student_stmt)
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    guard_stmt = select(Guardianship).where(Guardianship.parent_id == parent.id, Guardianship.student_id == student.id)
    guard_result = await session.execute(guard_stmt)
    if guard_result.scalar_one_or_none():
        return student

    guardianship = Guardianship(parent_id=parent.id, student_id=student.id)
    session.add(guardianship)
    await session.commit()
    return student


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
