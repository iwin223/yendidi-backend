from datetime import datetime, timedelta
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import func, select

from app.core.dependencies import get_current_user, get_session
from app.db.models import Order, OrderLine, User, Vendor, WalletTransaction
from app.db.session import AsyncSession

router = APIRouter()


class ExportResult(BaseModel):
    url: str


class StudentReportResponse(BaseModel):
    total_spent_minor: int
    orders_count: int
    average_order_minor: int
    last_ordered_at: datetime | None


class VendorReportResponse(BaseModel):
    revenue_minor: int
    orders_count: int
    average_order_minor: int
    best_seller_item_id: str | None


class SchoolReportResponse(BaseModel):
    total_value_minor: int
    orders_count: int
    unique_students: int
    top_vendor_id: str | None


class PlatformReportResponse(BaseModel):
    gmv_minor: int
    commission_minor: int
    mrr_minor: int
    school_leaderboard: List[dict]


@router.get("/reports/student/{student_id}", response_model=StudentReportResponse)
async def student_report(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"parent", "school_admin", "super_admin"} and current_user.role != "student":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    student_orders = select(func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0), func.coalesce(func.avg(Order.total_minor), 0), func.max(Order.placed_at)).where(Order.student_id == student_id)
    result = await session.execute(student_orders)
    count, total, average, last_ordered_at = result.one()
    return StudentReportResponse(
        total_spent_minor=int(total),
        orders_count=int(count),
        average_order_minor=int(average or 0),
        last_ordered_at=last_ordered_at,
    )


@router.get("/reports/vendor/{vendor_id}", response_model=VendorReportResponse)
async def vendor_report(
    vendor_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"vendor", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    revenue_stmt = select(func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0), func.coalesce(func.avg(Order.total_minor), 0)).where(Order.vendor_id == vendor_id)
    revenue_result = await session.execute(revenue_stmt)
    count, total, average = revenue_result.one()
    best_seller = select(OrderLine.menu_item_id, func.count(OrderLine.id).label("popularity")).join(Order, Order.id == OrderLine.order_id).where(Order.vendor_id == vendor_id).group_by(OrderLine.menu_item_id).order_by(func.count(OrderLine.id).desc()).limit(1)
    best_result = await session.execute(best_seller)
    best_row = best_result.first()
    return VendorReportResponse(
        revenue_minor=int(total),
        orders_count=int(count),
        average_order_minor=int(average or 0),
        best_seller_item_id=str(best_row[0]) if best_row else None,
    )


@router.get("/reports/school/{school_id}", response_model=SchoolReportResponse)
async def school_report(
    school_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    order_stmt = select(func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0)).where(Order.school_id == school_id)
    order_result = await session.execute(order_stmt)
    count, total = order_result.one()
    student_stmt = select(func.count(func.distinct(Order.student_id))).where(Order.school_id == school_id)
    student_result = await session.execute(student_stmt)
    unique_students = student_result.scalar_one()
    top_vendor_stmt = select(Order.vendor_id, func.sum(Order.total_minor).label("revenue")).where(Order.school_id == school_id).group_by(Order.vendor_id).order_by(func.sum(Order.total_minor).desc()).limit(1)
    top_vendor_result = await session.execute(top_vendor_stmt)
    top_vendor_row = top_vendor_result.first()
    return SchoolReportResponse(
        total_value_minor=int(total),
        orders_count=int(count),
        unique_students=int(unique_students),
        top_vendor_id=str(top_vendor_row[0]) if top_vendor_row else None,
    )


@router.get("/reports/platform", response_model=PlatformReportResponse)
async def platform_report(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    gmv_stmt = select(func.coalesce(func.sum(Order.total_minor), 0))
    gmv = await session.execute(gmv_stmt)
    gmv_total = gmv.scalar_one()
    commission = int(gmv_total * 0.05)
    leaderboard_stmt = select(Order.school_id, func.coalesce(func.sum(Order.total_minor), 0).label("revenue")).group_by(Order.school_id).order_by(func.sum(Order.total_minor).desc()).limit(5)
    leaderboard_result = await session.execute(leaderboard_stmt)
    leaderboard = [
        {"school_id": str(row[0]), "revenue_minor": int(row[1])}
        for row in leaderboard_result.fetchall()
    ]
    return PlatformReportResponse(
        gmv_minor=int(gmv_total),
        commission_minor=commission,
        mrr_minor=0,
        school_leaderboard=leaderboard,
    )


@router.post("/reports/{scope}/{entity_id}/export", response_model=ExportResult)
async def export_report(
    scope: Literal["student", "vendor", "school", "platform"],
    entity_id: UUID,
    current_user: User = Depends(get_current_user),
):
    if scope == "platform" and current_user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if scope == "school" and current_user.role not in {"school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if scope == "vendor" and current_user.role not in {"vendor", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if scope == "student":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return ExportResult(url=f"https://example.com/reports/{scope}/{entity_id}.csv")


class RecommendationResponse(BaseModel):
    menu_item_id: str
    score: float
    reason: str


class StudentInsightResponse(BaseModel):
    spending_last_7_days_minor: int
    orders_last_30_days: int
    favorite_vendor_id: Optional[str]


class VendorForecastResponse(BaseModel):
    projected_revenue_minor: int
    expected_orders_next_week: int


@router.get("/students/{student_id}/recommendations", response_model=List[RecommendationResponse])
async def student_recommendations(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"student", "parent", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    query = select(OrderLine.menu_item_id, func.count(OrderLine.id).label("count"))
    query = query.join(Order, Order.id == OrderLine.order_id).where(Order.student_id == student_id).group_by(OrderLine.menu_item_id).order_by(func.count(OrderLine.id).desc()).limit(5)
    result = await session.execute(query)
    recommendations = [
        RecommendationResponse(menu_item_id=str(row[0]), score=float(row[1]), reason="You order this often")
        for row in result.fetchall()
    ]
    return recommendations


@router.get("/students/{student_id}/insights", response_model=StudentInsightResponse)
async def student_insights(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"student", "parent", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    date_from = datetime.utcnow() - timedelta(days=7)
    stmt = select(func.coalesce(func.sum(Order.total_minor), 0), func.count(Order.id)).where(Order.student_id == student_id, Order.placed_at >= date_from)
    sum_result = await session.execute(stmt)
    spent, count = sum_result.one()
    fav_stmt = select(Order.vendor_id, func.count(Order.id).label("count")).where(Order.student_id == student_id).group_by(Order.vendor_id).order_by(func.count(Order.id).desc()).limit(1)
    fav_result = await session.execute(fav_stmt)
    fav_row = fav_result.first()
    return StudentInsightResponse(
        spending_last_7_days_minor=int(spent),
        orders_last_30_days=int(count),
        favorite_vendor_id=str(fav_row[0]) if fav_row else None,
    )


@router.get("/vendors/{vendor_id}/forecast", response_model=VendorForecastResponse)
async def vendor_forecast(
    vendor_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"vendor", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.role == "vendor":
        vendor_stmt = select(Vendor).where(Vendor.user_id == current_user.id)
        vendor_result = await session.execute(vendor_stmt)
        vendor = vendor_result.scalar_one_or_none()
        if not vendor or vendor.id != vendor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    revenue_stmt = select(func.coalesce(func.sum(Order.total_minor), 0)).where(Order.vendor_id == vendor_id)
    revenue_result = await session.execute(revenue_stmt)
    revenue = revenue_result.scalar_one()
    return VendorForecastResponse(
        projected_revenue_minor=int(revenue * 1.1),
        expected_orders_next_week=10,
    )
