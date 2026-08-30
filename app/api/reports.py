from datetime import datetime, timedelta
from typing import Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_
from sqlmodel import func, select

from app.core.analytics import (
    CATEGORY_LABEL,
    Period,
    delta_percent,
    format_day_label,
    format_money,
    format_short_date,
    period_start,
    period_window_days,
    start_of_day,
    start_of_month,
)
from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    Invoice,
    InvoiceStatus,
    MenuItem,
    Order,
    OrderLine,
    OrderStatus,
    Parent,
    School,
    SchoolStatus,
    Student,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    User,
    Vendor,
    VendorSchool,
    VendorStatus,
    Wallet,
)
from app.db.session import AsyncSession

# Orders that represent real revenue — mirrors the client's own `isSettled()`
# (src/data/api/analytics.ts). A cancelled or rejected order is refunded in
# full, so counting it toward spend/revenue inflates every figure below by
# however much got rejected or cancelled.
SETTLED_STATUSES = [s for s in OrderStatus if s not in (OrderStatus.cancelled, OrderStatus.rejected)]

router = APIRouter()


# ------------------------------------------------------------------ #
# Shared fetch + aggregation helpers
#
# These are a server-side port of src/data/api/analytics.ts and
# src/data/api/insights.ts — same windows, same weighting, same category
# labels. Order volume here is bounded by a school's own order history, not
# internet-scale, so "fetch the window into Python and aggregate there" (the
# same non-clever tradeoff the client's own forecast comment makes) is both
# simpler to keep correct and fast enough.
# ------------------------------------------------------------------ #


class _OrderRow:
    __slots__ = (
        "id", "student_id", "vendor_id", "school_id", "status",
        "total_minor", "service_fee_minor", "placed_at", "lines",
    )

    def __init__(self, order: Order):
        self.id = order.id
        self.student_id = order.student_id
        self.vendor_id = order.vendor_id
        self.school_id = order.school_id
        self.status = order.status
        self.total_minor = order.total_minor
        self.service_fee_minor = order.service_fee_minor
        self.placed_at = order.placed_at
        self.lines: List[OrderLine] = []


async def _fetch_orders_with_lines(session: AsyncSession, where_clause, since: datetime) -> List[_OrderRow]:
    stmt = select(Order).where(where_clause, Order.placed_at >= since)
    result = await session.execute(stmt)
    orders = result.scalars().all()
    if not orders:
        return []
    order_ids = [o.id for o in orders]
    lines_result = await session.execute(select(OrderLine).where(OrderLine.order_id.in_(order_ids)))
    lines_by_order: Dict[UUID, List[OrderLine]] = {}
    for line in lines_result.scalars().all():
        lines_by_order.setdefault(line.order_id, []).append(line)
    rows = []
    for o in orders:
        row = _OrderRow(o)
        row.lines = lines_by_order.get(o.id, [])
        rows.append(row)
    return rows


def _is_settled(o: _OrderRow) -> bool:
    return o.status not in (OrderStatus.cancelled, OrderStatus.rejected)


def _js_day_of_week(dt: datetime) -> int:
    """JS `Date.getDay()` convention (Sunday=0) from Python's Monday=0 weekday()."""
    return (dt.weekday() + 1) % 7


def _summarise(orders: List[_OrderRow]) -> dict:
    settled = [o for o in orders if _is_settled(o)]
    revenue = sum(o.total_minor for o in settled)
    return {
        "revenue_minor": revenue,
        "orders_count": len(settled),
        "average_order_minor": round(revenue / len(settled)) if settled else 0,
        "unique_students": len({o.student_id for o in settled}),
    }


def _revenue_by_day(orders: List[_OrderRow], days: int, now: datetime) -> List[dict]:
    today = start_of_day(now)
    out = []
    for i in range(days - 1, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        total = sum(o.total_minor for o in orders if _is_settled(o) and day_start <= o.placed_at < day_end)
        label = format_day_label(day_start) if days <= 7 else format_short_date(day_start)
        out.append({"label": label, "value": total})
    return out


def _orders_by_hour(orders: List[_OrderRow]) -> List[dict]:
    buckets: Dict[int, int] = {}
    for o in orders:
        if not _is_settled(o):
            continue
        buckets[o.placed_at.hour] = buckets.get(o.placed_at.hour, 0) + 1
    if not buckets:
        return []
    lo, hi = min(buckets), max(buckets)
    out = []
    for h in range(lo, hi + 1):
        h12 = 12 if h % 12 == 0 else h % 12
        out.append({"label": f"{h12}{'p' if h >= 12 else 'a'}", "value": buckets.get(h, 0)})
    return out


def _top_items(orders: List[_OrderRow], limit: int = 6) -> List[dict]:
    tally: Dict[str, dict] = {}
    for o in orders:
        if not _is_settled(o):
            continue
        for line in o.lines:
            entry = tally.setdefault(line.name, {"units": 0, "revenue": 0})
            entry["units"] += line.quantity
            entry["revenue"] += line.unit_price_minor * line.quantity
    items = [{"label": k, "value": v["units"], "revenue_minor": v["revenue"]} for k, v in tally.items()]
    items.sort(key=lambda x: x["value"], reverse=True)
    return items[:limit]


def _spend_by_category(orders: List[_OrderRow], categories_by_item: Dict[str, str]) -> List[dict]:
    tally: Dict[str, int] = {}
    for o in orders:
        if not _is_settled(o):
            continue
        for line in o.lines:
            if not line.menu_item_id:
                continue
            cat = categories_by_item.get(str(line.menu_item_id))
            if not cat:
                continue
            tally[cat] = tally.get(cat, 0) + line.unit_price_minor * line.quantity
    items = [{"category": cat, "label": CATEGORY_LABEL.get(cat, cat), "value": value} for cat, value in tally.items()]
    items.sort(key=lambda x: x["value"], reverse=True)
    return items


def _best_seller_item_id(orders: List[_OrderRow]) -> Optional[str]:
    tally: Dict[str, int] = {}
    for o in orders:
        if not _is_settled(o):
            continue
        for line in o.lines:
            if not line.menu_item_id:
                continue
            key = str(line.menu_item_id)
            tally[key] = tally.get(key, 0) + line.quantity
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]


def _revenue_trend(orders: List[_OrderRow], window_days: int, now: datetime) -> dict:
    today = start_of_day(now)
    current_from = today - timedelta(days=window_days - 1)
    previous_from = current_from - timedelta(days=window_days)

    def _sum(frm: datetime, to: datetime) -> int:
        return sum(o.total_minor for o in orders if _is_settled(o) and frm <= o.placed_at < to)

    current = _sum(current_from, today + timedelta(days=1))
    previous = _sum(previous_from, current_from)
    return {"current_minor": current, "previous_minor": previous, "delta_percent": delta_percent(current, previous)}


# ------------------------------------------------------------------ #
# Response models
# ------------------------------------------------------------------ #


class ExportResult(BaseModel):
    url: str


class SeriesPoint(BaseModel):
    label: str
    value: int


class TopItem(BaseModel):
    label: str
    value: int
    revenue_minor: int


class CategorySpend(BaseModel):
    category: str
    label: str
    value: int


class PeriodSummary(BaseModel):
    revenue_minor: int
    orders_count: int
    average_order_minor: int


class StudentReportResponse(BaseModel):
    total_spent_minor: int
    orders_count: int
    average_order_minor: int
    last_ordered_at: datetime | None
    period_summary: PeriodSummary
    revenue_by_day: List[SeriesPoint]
    spend_by_category: List[CategorySpend]
    top_items: List[TopItem]


class RevenueTrend(BaseModel):
    current_minor: int
    previous_minor: int
    delta_percent: Optional[int]


class VendorReportResponse(BaseModel):
    revenue_minor: int
    orders_count: int
    average_order_minor: int
    unique_students: int
    revenue_trend: RevenueTrend
    revenue_by_day: List[SeriesPoint]
    orders_by_hour: List[SeriesPoint]
    top_items: List[TopItem]
    best_seller_item_id: Optional[str]


class VendorPerformanceItem(BaseModel):
    vendor_id: str
    name: str
    orders: int
    revenue_minor: int
    rating: float
    rejection_rate: int


class SchoolReportResponse(BaseModel):
    total_value_minor: int
    orders_count: int
    unique_students: int
    top_vendor_id: Optional[str]
    vendor_performance: List[VendorPerformanceItem]
    top_items: List[TopItem]


class SchoolLeaderboardItem(BaseModel):
    school_id: str
    name: str
    orders: int
    revenue_minor: int
    students: int


class PlatformReportResponse(BaseModel):
    schools: int
    active_schools: int
    trial_schools: int
    vendors: int
    pending_vendors: int
    students: int
    parents: int
    orders_this_month: int
    gmv_this_month_minor: int
    platform_revenue_this_month_minor: int
    mrr_minor: int
    school_leaderboard: List[SchoolLeaderboardItem]


class RecommendationResponse(BaseModel):
    menu_item_id: str
    score: float
    reason: str


class SpendingInsightItem(BaseModel):
    id: str
    tone: str
    title: str
    detail: str


class ForecastPrediction(BaseModel):
    name: str
    item_id: str
    expected_units: int
    stock_count: int
    shortfall: int


class VendorForecastResponse(BaseModel):
    peak_hour_label: str
    peak_hour_share: int
    predictions: List[ForecastPrediction]
    advice: List[str]


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.get("/reports/student/{student_id}", response_model=StudentReportResponse)
async def student_report(
    student_id: UUID,
    period: Period = Query("month", description="Scopes period_summary/revenue_by_day/spend_by_category/top_items only — the top-level totals are always all-time."),
    days: int = Query(14, ge=1, le=90, description="Length of the daily revenue series."),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"parent", "school_admin", "super_admin"} and current_user.role != "student":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    student_orders = select(
        func.count(Order.id),
        func.coalesce(func.sum(Order.total_minor), 0),
        func.coalesce(func.avg(Order.total_minor), 0),
        func.max(Order.placed_at),
    ).where(Order.student_id == student_id, Order.status.in_(SETTLED_STATUSES))
    result = await session.execute(student_orders)
    count, total, average, last_ordered_at = result.one()

    now = datetime.utcnow()
    window_start = period_start(period, now)
    fetch_since = min(window_start, start_of_day(now) - timedelta(days=days - 1))
    orders = await _fetch_orders_with_lines(session, Order.student_id == student_id, fetch_since)
    period_orders = [o for o in orders if o.placed_at >= window_start]
    period_summary = _summarise(period_orders)

    menu_item_ids = {line.menu_item_id for o in period_orders for line in o.lines if line.menu_item_id}
    categories_by_item: Dict[str, str] = {}
    if menu_item_ids:
        mi_result = await session.execute(select(MenuItem.id, MenuItem.category).where(MenuItem.id.in_(menu_item_ids)))
        for mid, cat in mi_result.all():
            categories_by_item[str(mid)] = getattr(cat, "value", cat)

    return StudentReportResponse(
        total_spent_minor=int(total),
        orders_count=int(count),
        average_order_minor=int(average or 0),
        last_ordered_at=last_ordered_at,
        period_summary=PeriodSummary(
            revenue_minor=period_summary["revenue_minor"],
            orders_count=period_summary["orders_count"],
            average_order_minor=period_summary["average_order_minor"],
        ),
        revenue_by_day=[SeriesPoint(**p) for p in _revenue_by_day(orders, days, now)],
        spend_by_category=[CategorySpend(**c) for c in _spend_by_category(period_orders, categories_by_item)],
        top_items=[TopItem(**i) for i in _top_items(period_orders, 5)],
    )


@router.get("/reports/vendor/{vendor_id}", response_model=VendorReportResponse)
async def vendor_report(
    vendor_id: UUID,
    period: Period = Query("week"),
    days: int = Query(7, ge=1, le=90, description="Length of the daily revenue series."),
    school_id: Optional[UUID] = Query(
        None,
        description="Scope the whole report to orders placed at one school only — used by a school's own vendor-detail view (e.g. 'most ordered at your school').",
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"vendor", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if school_id is not None and current_user.role == "school_admin" and current_user.school_id != school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    now = datetime.utcnow()
    window_start = period_start(period, now)
    trend_window_days = period_window_days(period)
    fetch_since = min(window_start, start_of_day(now) - timedelta(days=max(days, trend_window_days * 2) - 1))

    where_clause = (
        and_(Order.vendor_id == vendor_id, Order.school_id == school_id)
        if school_id is not None
        else Order.vendor_id == vendor_id
    )
    orders = await _fetch_orders_with_lines(session, where_clause, fetch_since)
    period_orders = [o for o in orders if o.placed_at >= window_start]
    summary = _summarise(period_orders)

    return VendorReportResponse(
        revenue_minor=summary["revenue_minor"],
        orders_count=summary["orders_count"],
        average_order_minor=summary["average_order_minor"],
        unique_students=summary["unique_students"],
        revenue_trend=RevenueTrend(**_revenue_trend(orders, trend_window_days, now)),
        revenue_by_day=[SeriesPoint(**p) for p in _revenue_by_day(orders, days, now)],
        orders_by_hour=[SeriesPoint(**p) for p in _orders_by_hour(period_orders)],
        top_items=[TopItem(**i) for i in _top_items(period_orders)],
        best_seller_item_id=_best_seller_item_id(period_orders),
    )


@router.get("/reports/school/{school_id}", response_model=SchoolReportResponse)
async def school_report(
    school_id: UUID,
    period: Period = Query("week"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    now = datetime.utcnow()
    window_start = period_start(period, now)
    orders = await _fetch_orders_with_lines(session, Order.school_id == school_id, window_start)
    summary = _summarise(orders)

    vendor_ids_result = await session.execute(select(VendorSchool.vendor_id).where(VendorSchool.school_id == school_id))
    vendor_ids = vendor_ids_result.scalars().all()
    vendors: List[Vendor] = []
    if vendor_ids:
        vendors_result = await session.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
        vendors = vendors_result.scalars().all()

    orders_by_vendor: Dict[str, List[_OrderRow]] = {}
    for o in orders:
        orders_by_vendor.setdefault(str(o.vendor_id), []).append(o)

    vendor_perf = []
    for v in vendors:
        v_orders = orders_by_vendor.get(str(v.id), [])
        settled = [o for o in v_orders if _is_settled(o)]
        rejected = len([o for o in v_orders if o.status == OrderStatus.rejected])
        vendor_perf.append({
            "vendor_id": str(v.id),
            "name": v.business_name,
            "orders": len(settled),
            "revenue_minor": sum(o.total_minor for o in settled),
            "rating": v.rating,
            "rejection_rate": round((rejected / len(v_orders)) * 100) if v_orders else 0,
        })
    vendor_perf.sort(key=lambda x: x["revenue_minor"], reverse=True)
    top_vendor_id = vendor_perf[0]["vendor_id"] if vendor_perf and vendor_perf[0]["revenue_minor"] > 0 else None

    return SchoolReportResponse(
        total_value_minor=summary["revenue_minor"],
        orders_count=summary["orders_count"],
        unique_students=summary["unique_students"],
        top_vendor_id=top_vendor_id,
        vendor_performance=[VendorPerformanceItem(**v) for v in vendor_perf],
        top_items=[TopItem(**i) for i in _top_items(orders)],
    )


@router.get("/reports/platform", response_model=PlatformReportResponse)
async def platform_report(
    period: Period = Query("month", description="Scopes the school leaderboard only — the summary tiles are always the current month, matching the client."),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    now = datetime.utcnow()
    month_start = start_of_month(now)

    schools_count = (await session.execute(select(func.count(School.id)))).scalar_one()
    active_schools = (await session.execute(select(func.count(School.id)).where(School.status == SchoolStatus.active))).scalar_one()
    trial_schools = (await session.execute(select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.trialing))).scalar_one()
    vendors_count = (await session.execute(select(func.count(Vendor.id)))).scalar_one()
    pending_vendors = (await session.execute(select(func.count(Vendor.id)).where(Vendor.status == VendorStatus.pending))).scalar_one()
    students_count = (await session.execute(select(func.count(Student.id)))).scalar_one()
    parents_count = (await session.execute(select(func.count(Parent.id)))).scalar_one()

    month_orders_result = await session.execute(
        select(Order).where(Order.placed_at >= month_start, Order.status.in_(SETTLED_STATUSES))
    )
    month_orders = month_orders_result.scalars().all()
    gmv = sum(o.total_minor for o in month_orders)
    commission = sum(o.service_fee_minor for o in month_orders)

    invoices_result = await session.execute(
        select(Invoice).where(Invoice.status == InvoiceStatus.paid, Invoice.created_at >= month_start)
    )
    subscription_revenue = sum(i.amount_minor for i in invoices_result.scalars().all())

    active_subs_result = await session.execute(select(Subscription).where(Subscription.status == SubscriptionStatus.active))
    # Annual plans are normalised to a monthly figure so the two plans compare on one line.
    mrr = sum(
        (round(s.amount_minor / 12) if s.plan == SubscriptionPlan.annual else s.amount_minor)
        for s in active_subs_result.scalars().all()
    )

    leaderboard_start = period_start(period, now)
    leaderboard_orders_result = await session.execute(
        select(Order).where(Order.placed_at >= leaderboard_start, Order.status.in_(SETTLED_STATUSES))
    )
    by_school: Dict[str, List[Order]] = {}
    for o in leaderboard_orders_result.scalars().all():
        by_school.setdefault(str(o.school_id), []).append(o)

    all_schools_result = await session.execute(select(School))
    all_schools = all_schools_result.scalars().all()
    student_counts_result = await session.execute(select(Student.school_id, func.count(Student.id)).group_by(Student.school_id))
    student_counts = {str(k): v for k, v in student_counts_result.all()}

    leaderboard = []
    for s in all_schools:
        s_orders = by_school.get(str(s.id), [])
        leaderboard.append({
            "school_id": str(s.id),
            "name": s.name,
            "orders": len(s_orders),
            "revenue_minor": sum(o.total_minor for o in s_orders),
            "students": student_counts.get(str(s.id), 0),
        })
    leaderboard.sort(key=lambda x: x["revenue_minor"], reverse=True)

    return PlatformReportResponse(
        schools=schools_count,
        active_schools=active_schools,
        trial_schools=trial_schools,
        vendors=vendors_count,
        pending_vendors=pending_vendors,
        students=students_count,
        parents=parents_count,
        orders_this_month=len(month_orders),
        gmv_this_month_minor=gmv,
        platform_revenue_this_month_minor=commission + subscription_revenue,
        mrr_minor=mrr,
        school_leaderboard=[SchoolLeaderboardItem(**x) for x in leaderboard[:10]],
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


@router.get("/students/{student_id}/recommendations", response_model=List[RecommendationResponse])
async def student_recommendations(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"student", "parent", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    query = (
        select(OrderLine.menu_item_id, func.count(OrderLine.id).label("count"))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.student_id == student_id)
        .group_by(OrderLine.menu_item_id)
        .order_by(func.count(OrderLine.id).desc())
        .limit(5)
    )
    result = await session.execute(query)
    return [
        RecommendationResponse(menu_item_id=str(row[0]), score=float(row[1]), reason="You order this often")
        for row in result.fetchall()
    ]


@router.get("/students/{student_id}/insights", response_model=List[SpendingInsightItem])
async def student_insights(
    student_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role not in {"student", "parent", "school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    student = (await session.execute(select(Student).where(Student.id == student_id))).scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    now = datetime.utcnow()
    today = start_of_day(now)
    this_week_from = today - timedelta(days=6)
    last_week_from = today - timedelta(days=13)
    month_from = today - timedelta(days=30)

    orders = await _fetch_orders_with_lines(session, Order.student_id == student_id, month_from)
    settled = [o for o in orders if _is_settled(o)]
    month_orders = [o for o in settled if o.placed_at >= month_from]

    insights: List[dict] = []

    def _sum(frm: datetime, to: datetime) -> int:
        return sum(o.total_minor for o in settled if frm <= o.placed_at < to)

    this_week = _sum(this_week_from, today + timedelta(days=1))
    last_week = _sum(last_week_from, this_week_from)

    if last_week > 0:
        change = round(((this_week - last_week) / last_week) * 100)
        if abs(change) >= 12:
            insights.append({
                "id": "trend",
                "tone": "watch" if change > 0 else "good",
                "title": (
                    f"Spending is up {change}% this week"
                    if change > 0
                    else f"Spending is down {abs(change)}% this week"
                ),
                "detail": f"{format_money(this_week)} this week versus {format_money(last_week)} last week.",
            })

    # Diet mix over the last month.
    menu_item_ids = {line.menu_item_id for o in month_orders for line in o.lines if line.menu_item_id}
    categories_by_item: Dict[str, str] = {}
    if menu_item_ids:
        mi_result = await session.execute(select(MenuItem.id, MenuItem.category).where(MenuItem.id.in_(menu_item_ids)))
        for mid, cat in mi_result.all():
            categories_by_item[str(mid)] = getattr(cat, "value", cat)

    category_tally: Dict[str, int] = {}
    for o in month_orders:
        for line in o.lines:
            if not line.menu_item_id:
                continue
            cat = categories_by_item.get(str(line.menu_item_id))
            if not cat:
                continue
            category_tally[cat] = category_tally.get(cat, 0) + line.unit_price_minor * line.quantity

    month_total = sum(category_tally.values())
    by_category_sorted = sorted(category_tally.items(), key=lambda kv: kv[1], reverse=True)

    if month_total > 0:
        healthy = category_tally.get("healthy", 0) + category_tally.get("fruits", 0)
        treats = category_tally.get("snacks", 0) + category_tally.get("desserts", 0) + category_tally.get("drinks", 0)
        healthy_pct = round((healthy / month_total) * 100)
        treat_pct = round((treats / month_total) * 100)

        if treat_pct >= 40:
            insights.append({
                "id": "treats",
                "tone": "watch",
                "title": f"{treat_pct}% of spending went on snacks, drinks and desserts",
                "detail": "You can restrict specific categories from Spending Controls without freezing the wallet.",
            })
        elif healthy_pct >= 25:
            insights.append({
                "id": "healthy",
                "tone": "good",
                "title": f"{healthy_pct}% of spending went on fruit and healthy meals",
                "detail": f"{student.first_name} is choosing well this month.",
            })

        if by_category_sorted:
            top_cat, top_val = by_category_sorted[0]
            insights.append({
                "id": "top-category",
                "tone": "neutral",
                "title": f"Most spending: {CATEGORY_LABEL.get(top_cat, top_cat)}",
                "detail": f"{format_money(top_val)} over the last 30 days.",
            })

    # Repeat purchase — the "they eat the same thing every day" observation.
    item_counts: Dict[str, int] = {}
    for o in month_orders:
        for line in o.lines:
            item_counts[line.name] = item_counts.get(line.name, 0) + line.quantity
    if item_counts:
        fav_name, fav_count = max(item_counts.items(), key=lambda kv: kv[1])
        if fav_count >= 5:
            insights.append({
                "id": "favourite",
                "tone": "neutral",
                "title": f"{fav_name} ordered {fav_count} times",
                "detail": f"It is {student.first_name}'s most-ordered item this month.",
            })

    wallet = (await session.execute(select(Wallet).where(Wallet.student_id == student_id))).scalar_one_or_none()
    if wallet and wallet.balance_minor <= wallet.low_balance_threshold_minor:
        insights.append({
            "id": "low-balance",
            "tone": "watch",
            "title": "Wallet balance is low",
            "detail": f"{format_money(wallet.balance_minor)} remaining. Top up to avoid a missed lunch.",
        })

    return [SpendingInsightItem(**i) for i in insights]


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

    now = datetime.utcnow()
    lookback = start_of_day(now) - timedelta(days=28)
    orders = await _fetch_orders_with_lines(session, Order.vendor_id == vendor_id, lookback)
    settled = [o for o in orders if _is_settled(o)]

    hour_counts: Dict[int, int] = {}
    for o in settled:
        hour_counts[o.placed_at.hour] = hour_counts.get(o.placed_at.hour, 0) + 1
    total_orders = len(settled) or 1
    if hour_counts:
        peak_hour, peak_count = max(hour_counts.items(), key=lambda kv: kv[1])
        peak_hour_share = round((peak_count / total_orders) * 100)
    else:
        peak_hour, peak_hour_share = 12, 0
    peak_hour_label = f"{12 if peak_hour % 12 == 0 else peak_hour % 12}{'pm' if peak_hour >= 12 else 'am'}"

    # Tomorrow's weekday, skipping the weekend since schools are closed.
    tomorrow = now + timedelta(days=1)
    target_day = _js_day_of_week(tomorrow)
    if target_day in (0, 6):  # Sunday or Saturday
        target_day = 1  # Monday

    units_by_item: Dict[str, dict] = {}
    for o in settled:
        day_key = start_of_day(o.placed_at)
        order_day = _js_day_of_week(o.placed_at)
        for line in o.lines:
            if not line.menu_item_id:
                continue
            key = str(line.menu_item_id)
            entry = units_by_item.setdefault(key, {"total": 0, "same_day": 0, "days": set(), "same_day_count": 0})
            entry["total"] += line.quantity
            entry["days"].add(day_key)
            if order_day == target_day:
                entry["same_day"] += line.quantity
                entry["same_day_count"] += 1

    items_result = await session.execute(select(MenuItem).where(MenuItem.vendor_id == vendor_id))
    items = items_result.scalars().all()

    predictions = []
    for item in items:
        entry = units_by_item.get(str(item.id))
        if not entry or not entry["days"]:
            predictions.append({
                "name": item.name, "item_id": str(item.id),
                "expected_units": 0, "stock_count": item.stock_count, "shortfall": 0,
            })
            continue
        overall_mean = entry["total"] / len(entry["days"])
        same_day_mean = entry["same_day"] / max(1, entry["same_day_count"]) if entry["same_day_count"] > 0 else overall_mean
        # 60/40 blend: the weekday signal matters but four samples is thin.
        expected_units = round(same_day_mean * 0.6 + overall_mean * 0.4)
        predictions.append({
            "name": item.name,
            "item_id": str(item.id),
            "expected_units": expected_units,
            "stock_count": item.stock_count,
            "shortfall": max(0, expected_units - item.stock_count),
        })

    predictions = [p for p in predictions if p["expected_units"] > 0]
    predictions.sort(key=lambda p: p["expected_units"], reverse=True)

    advice = []
    for s in [p for p in predictions if p["shortfall"] > 0][:3]:
        advice.append(
            f"Add at least {s['shortfall']} more {s['name']} — you have {s['stock_count']} and usually sell {s['expected_units']}."
        )
    if peak_hour_share >= 30:
        advice.append(f"{peak_hour_share}% of your orders land around {peak_hour_label}. Have that batch ready before then.")
    if not advice:
        advice.append("Stock levels look sufficient for tomorrow based on the last four weeks.")

    return VendorForecastResponse(
        peak_hour_label=peak_hour_label,
        peak_hour_share=peak_hour_share,
        predictions=[ForecastPrediction(**p) for p in predictions[:6]],
        advice=advice,
    )
