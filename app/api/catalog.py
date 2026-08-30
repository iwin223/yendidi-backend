from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlmodel import delete, select

from app.core.dependencies import get_current_user, get_current_user_optional, get_session
from app.db.models import (
    FoodCategory,
    MenuItem,
    Role,
    Vendor,
    VendorCertification,
    VendorPayout,
    VendorSchool,
    VendorStatus,
    User,
)
from app.db.session import AsyncSession

router = APIRouter()


class VendorSummary(BaseModel):
    id: UUID
    business_name: str
    owner_name: str
    rating: float
    accepting_orders: bool

    class Config:
        from_attributes = True


class VendorDetail(VendorSummary):
    phone: str
    email: Optional[str]
    description: Optional[str]


class MenuItemResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    name: str
    description: Optional[str]
    category: FoodCategory
    tags: List[FoodCategory]
    price_minor: int
    art_key: str
    ingredients: List[str]
    available: bool
    stock_count: int
    prep_minutes: int
    kcal: Optional[int]

    class Config:
        from_attributes = True


class MenuItemCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    category: FoodCategory
    tags: Optional[List[FoodCategory]] = []
    price_minor: int
    art_key: str
    ingredients: Optional[List[str]] = []
    available: bool = True
    stock_count: int = 0
    prep_minutes: int = 10
    kcal: Optional[int] = None


class MenuItemUpdateRequest(BaseModel):
    # Every field is genuinely optional here — this backs a PATCH that relies
    # on `.dict(exclude_unset=True)` for partial updates. Without `= None`,
    # Pydantic v2 still treats `Optional[X]` as required (just nullable), so a
    # caller omitting any field — the entire point of a partial update — got a
    # 422 "field required" instead.
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[FoodCategory] = None
    tags: Optional[List[FoodCategory]] = None
    price_minor: Optional[int] = None
    art_key: Optional[str] = None
    ingredients: Optional[List[str]] = None
    available: Optional[bool] = None
    stock_count: Optional[int] = None
    prep_minutes: Optional[int] = None
    kcal: Optional[int] = None


class VendorAcceptingUpdate(BaseModel):
    accepting_orders: bool


class VendorStatusUpdateRequest(BaseModel):
    status: VendorStatus
    reason: Optional[str] = None


class VendorSchoolsUpdateRequest(BaseModel):
    school_ids: List[UUID]


class VendorPayoutResponse(BaseModel):
    id: UUID
    amount_minor: int
    status: str
    reference: Optional[str]
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True


class VendorCertificationResponse(BaseModel):
    id: UUID
    name: str
    authority: str
    number: str
    document_key: Optional[str]
    issued_at: datetime
    expires_at: datetime
    verified: bool
    verified_by: Optional[UUID]
    verified_at: Optional[datetime]

    class Config:
        from_attributes = True


@router.get("/vendors", response_model=List[VendorSummary])
async def list_vendors(
    school_id: Optional[UUID] = Query(None),
    vendor_status: Optional[VendorStatus] = Query(None),
    category: Optional[FoodCategory] = Query(None),
    search: Optional[str] = Query(None),
    current_user: Optional[User] = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
):
    if vendor_status is not None:
        if not current_user or current_user.role != Role.super_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins may filter vendors by status")
        query = select(Vendor).where(Vendor.status == vendor_status)
    else:
        query = select(Vendor).where(Vendor.status == VendorStatus.approved)
    if school_id:
        vendor_ids = select(VendorSchool.vendor_id).where(VendorSchool.school_id == school_id)
        query = query.where(Vendor.id.in_(vendor_ids))
    if search:
        query = query.where(
            Vendor.business_name.ilike(f"%{search}%") | Vendor.owner_name.ilike(f"%{search}%")
        )
    result = await session.execute(query)
    return result.scalars().all()


@router.patch("/vendors/{vendor_id}/status")
async def set_vendor_status(
    vendor_id: UUID,
    request: VendorStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may update vendor status")
    query = select(Vendor).where(Vendor.id == vendor_id)
    result = await session.execute(query)
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    vendor.status = request.status
    session.add(vendor)
    await session.commit()
    return {"status": "ok", "vendor_status": vendor.status}


@router.post("/vendors/{vendor_id}/certifications", response_model=VendorCertificationResponse)
async def create_vendor_certification(
    vendor_id: UUID,
    name: str = Form(...),
    authority: str = Form(...),
    number: str = Form(...),
    expires_at: datetime = Form(...),
    file: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.vendor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors may submit certifications")
    vendor_stmt = select(Vendor).where(Vendor.id == vendor_id, Vendor.user_id == current_user.id)
    vendor_result = await session.execute(vendor_stmt)
    vendor = vendor_result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    document_key = file.filename if file else None
    certification = VendorCertification(
        id=uuid4(),
        vendor_id=vendor_id,
        name=name,
        authority=authority,
        number=number,
        document_key=document_key,
        issued_at=datetime.utcnow(),
        expires_at=expires_at,
    )
    session.add(certification)
    await session.commit()
    await session.refresh(certification)
    return certification


@router.patch("/certifications/{certification_id}/verify")
async def verify_vendor_certification(
    certification_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only platform admins may verify certifications")
    stmt = select(VendorCertification).where(VendorCertification.id == certification_id)
    result = await session.execute(stmt)
    certification = result.scalar_one_or_none()
    if not certification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certification not found")
    certification.verified = True
    certification.verified_by = current_user.id
    certification.verified_at = datetime.utcnow()
    session.add(certification)
    await session.commit()
    return {"status": "ok"}


@router.put("/vendors/{vendor_id}/schools")
async def update_vendor_schools(
    vendor_id: UUID,
    request: VendorSchoolsUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Vendor).where(Vendor.id == vendor_id)
    result = await session.execute(stmt)
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")

    existing_stmt = select(VendorSchool.school_id).where(VendorSchool.vendor_id == vendor_id)
    existing_result = await session.execute(existing_stmt)
    current_school_ids = set(existing_result.scalars().all())
    requested_school_ids = set(request.school_ids)

    if current_user.role == Role.school_admin:
        if not current_user.school_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to assign vendor schools")
        # This endpoint replaces the vendor's whole school list, so a school
        # admin is restricted to toggling only their own school's membership —
        # every other school's assignment in the request must come back
        # unchanged, or a school admin could rewrite a vendor's relationship
        # with a school they have nothing to do with.
        other_current = current_school_ids - {current_user.school_id}
        other_requested = requested_school_ids - {current_user.school_id}
        if other_current != other_requested:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="School admins may only change their own school's assignment to this vendor",
            )
    elif current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to assign vendor schools")

    await session.execute(delete(VendorSchool).where(VendorSchool.vendor_id == vendor_id))
    for school_id in request.school_ids:
        session.add(VendorSchool(vendor_id=vendor_id, school_id=school_id))
    await session.commit()
    return {"status": "ok", "school_ids": [str(s) for s in request.school_ids]}


@router.get("/vendors/{vendor_id}/payouts", response_model=List[VendorPayoutResponse])
async def get_vendor_payouts(
    vendor_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == Role.vendor:
        vendor_stmt = select(Vendor).where(Vendor.id == vendor_id, Vendor.user_id == current_user.id)
        vendor_result = await session.execute(vendor_stmt)
        if not vendor_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role != Role.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    payout_stmt = select(VendorPayout).where(VendorPayout.vendor_id == vendor_id).order_by(VendorPayout.created_at.desc())
    payout_result = await session.execute(payout_stmt)
    return payout_result.scalars().all()


@router.get("/schools/{school_id}/vendors", response_model=List[VendorSummary])
async def get_school_vendors(
    school_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {Role.student, Role.parent, Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    vendor_ids = select(VendorSchool.vendor_id).where(VendorSchool.school_id == school_id)
    query = select(Vendor).where(Vendor.id.in_(vendor_ids), Vendor.status == VendorStatus.approved)
    result = await session.execute(query)
    return result.scalars().all()


@router.get("/vendors/{vendor_id}", response_model=VendorDetail)
async def get_vendor(vendor_id: UUID, session: AsyncSession = Depends(get_session)):
    query = select(Vendor).where(Vendor.id == vendor_id, Vendor.status == VendorStatus.approved)
    result = await session.execute(query)
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    return vendor


@router.get("/vendors/{vendor_id}/menu", response_model=List[MenuItemResponse])
async def get_vendor_menu(vendor_id: UUID, session: AsyncSession = Depends(get_session)):
    query = select(MenuItem).where(MenuItem.vendor_id == vendor_id, MenuItem.available == True)
    result = await session.execute(query)
    return result.scalars().all()


@router.get("/schools/{school_id}/menu", response_model=List[MenuItemResponse])
async def get_school_menu(
    school_id: UUID,
    category: Optional[FoodCategory] = Query(None),
    q: Optional[str] = Query(None, alias="q"),
    available: bool = Query(True),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {Role.student, Role.parent, Role.school_admin, Role.super_admin}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    vendor_ids = select(VendorSchool.vendor_id).where(VendorSchool.school_id == school_id)
    approved_vendor_ids = select(Vendor.id).where(Vendor.id.in_(vendor_ids), Vendor.status == VendorStatus.approved)
    query = select(MenuItem).where(MenuItem.vendor_id.in_(approved_vendor_ids), MenuItem.available == available)
    if category:
        query = query.where(MenuItem.category == category)
    if q:
        query = query.where(
            MenuItem.name.ilike(f"%{q}%") |
            MenuItem.description.ilike(f"%{q}%") |
            MenuItem.ingredients.contains([q])
        )
    result = await session.execute(query)
    return result.scalars().all()


@router.post("/vendors/{vendor_id}/menu", response_model=MenuItemResponse)
async def create_menu_item(
    vendor_id: UUID,
    request: MenuItemCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.vendor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors may create menu items")
    query = select(Vendor).where(Vendor.id == vendor_id, Vendor.user_id == current_user.id)
    result = await session.execute(query)
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")

    item = MenuItem(
        id=uuid4(),
        vendor_id=vendor_id,
        name=request.name,
        description=request.description,
        category=request.category,
        tags=request.tags or [],
        price_minor=request.price_minor,
        art_key=request.art_key,
        ingredients=request.ingredients or [],
        available=request.available,
        stock_count=request.stock_count,
        prep_minutes=request.prep_minutes,
        kcal=request.kcal,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@router.patch("/menu-items/{item_id}", response_model=MenuItemResponse)
async def update_menu_item(
    item_id: UUID,
    request: MenuItemUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.vendor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors may update menu items")
    query = select(MenuItem).where(MenuItem.id == item_id)
    result = await session.execute(query)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Menu item not found")
    vendor_stmt = select(Vendor).where(Vendor.id == item.vendor_id, Vendor.user_id == current_user.id)
    vendor_result = await session.execute(vendor_stmt)
    if not vendor_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this menu item")

    update_data = request.dict(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(item, field_name, value)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@router.delete("/menu-items/{item_id}")
async def delete_menu_item(
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.vendor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors may delete menu items")
    query = select(MenuItem).where(MenuItem.id == item_id)
    result = await session.execute(query)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Menu item not found")
    vendor_stmt = select(Vendor).where(Vendor.id == item.vendor_id, Vendor.user_id == current_user.id)
    vendor_result = await session.execute(vendor_stmt)
    if not vendor_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this menu item")

    await session.delete(item)
    await session.commit()
    return {"status": "deleted"}


@router.patch("/vendors/{vendor_id}/accepting")
async def set_vendor_accepting(
    vendor_id: UUID,
    request: VendorAcceptingUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != Role.vendor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors may update accepting status")
    query = select(Vendor).where(Vendor.id == vendor_id, Vendor.user_id == current_user.id)
    result = await session.execute(query)
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")

    vendor.accepting_orders = request.accepting_orders
    session.add(vendor)
    await session.commit()
    return {"status": "ok", "accepting_orders": vendor.accepting_orders}


@router.get("/dish-catalog", response_model=List[MenuItemResponse])
async def dish_catalog(
    category: Optional[FoodCategory] = Query(None),
    search: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    query = select(MenuItem).where(MenuItem.available == True)
    if category:
        query = query.where(MenuItem.category == category)
    if search:
        query = query.where(MenuItem.name.ilike(f"%{search}%") | MenuItem.description.ilike(f"%{search}%"))
    result = await session.execute(query)
    return result.scalars().all()
