from datetime import datetime
from typing import List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import Favorite, FavoriteTargetType, Guardianship, Parent, Role, Student, User
from app.db.session import AsyncSession

router = APIRouter()


class FavoriteResponse(BaseModel):
    id: UUID
    student_id: UUID
    target_type: FavoriteTargetType
    target_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


class FavoriteCreateRequest(BaseModel):
    target_type: FavoriteTargetType
    target_id: UUID


async def _authorize_student_access(student_id: UUID, current_user: User, session: AsyncSession) -> None:
    if current_user.role == Role.student:
        stmt = select(Student).where(Student.id == student_id, Student.user_id == current_user.id)
        result = await session.execute(stmt)
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this student")
        return
    if current_user.role == Role.parent:
        parent_stmt = select(Parent).where(Parent.user_id == current_user.id)
        parent_result = await session.execute(parent_stmt)
        parent = parent_result.scalar_one_or_none()
        if parent:
            guard_stmt = select(Guardianship).where(Guardianship.parent_id == parent.id, Guardianship.student_id == student_id)
            guard_result = await session.execute(guard_stmt)
            if guard_result.scalar_one_or_none():
                return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this student")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this student")


@router.get("/students/{student_id}/favorites", response_model=List[FavoriteResponse])
async def list_favorites(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_student_access(student_id, current_user, session)
    stmt = select(Favorite).where(Favorite.student_id == student_id).order_by(Favorite.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/students/{student_id}/favorites", response_model=FavoriteResponse, status_code=status.HTTP_201_CREATED)
async def create_favorite(
    student_id: UUID,
    request: FavoriteCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_student_access(student_id, current_user, session)

    existing_stmt = select(Favorite).where(
        Favorite.student_id == student_id,
        Favorite.target_type == request.target_type,
        Favorite.target_id == request.target_id,
    )
    existing_result = await session.execute(existing_stmt)
    existing = existing_result.scalar_one_or_none()
    if existing:
        return existing

    favorite = Favorite(
        id=uuid4(),
        student_id=student_id,
        target_type=request.target_type,
        target_id=request.target_id,
    )
    session.add(favorite)
    await session.commit()
    await session.refresh(favorite)
    return favorite


@router.delete("/students/{student_id}/favorites/{target_id}")
async def delete_favorite(
    student_id: UUID,
    target_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_student_access(student_id, current_user, session)
    stmt = select(Favorite).where(Favorite.student_id == student_id, Favorite.target_id == target_id)
    result = await session.execute(stmt)
    favorite = result.scalar_one_or_none()
    if not favorite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorite not found")
    await session.delete(favorite)
    await session.commit()
    return {"status": "deleted"}
