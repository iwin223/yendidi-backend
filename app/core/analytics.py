"""Shared helpers for the /reports and /students/{id}/insights,
/vendors/{id}/forecast endpoints.

This is a server-side port of the client's `src/data/api/analytics.ts` and
`src/data/api/insights.ts` — same windows, same weighting, same category
labels — so that wiring these endpoints into the app changes only where the
numbers come from, not what they say. Where the client computes "local
device time" it explicitly assumes Africa/Accra (UTC+0, no DST) is the
device's clock; the backend already stores everything in naive UTC
(`datetime.utcnow()`), so plain UTC arithmetic here lines up with the
client's plain local-date arithmetic there.
"""

from datetime import datetime, timedelta
from typing import Literal, Optional

Period = Literal["today", "week", "month", "term"]

CURRENCY_SYMBOL = "GH₵"

DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CATEGORY_LABEL = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "snacks": "Snacks",
    "drinks": "Drinks",
    "desserts": "Desserts",
    "fruits": "Fruits",
    "healthy": "Healthy",
    "local": "Local Dishes",
}


def start_of_day(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_week(at: datetime) -> datetime:
    """School weeks run Monday-Sunday — `weekday()` is already Monday=0."""
    return start_of_day(at) - timedelta(days=at.weekday())


def start_of_month(at: datetime) -> datetime:
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def period_window_days(period: Period) -> int:
    """A fixed rolling-window length, distinct from `period_start`'s
    calendar-boundary meaning — used for revenue-trend comparisons
    ("last N days vs. the N before that"), not for "since the start of this
    calendar week/month". Mirrors VendorAnalyticsScreen.tsx's own mapping
    exactly (`period === 'today' ? 1 : ... : 91`)."""
    return {"today": 1, "week": 7, "month": 30, "term": 91}[period]


def period_start(period: Period, at: Optional[datetime] = None) -> datetime:
    at = at or datetime.utcnow()
    if period == "today":
        return start_of_day(at)
    if period == "week":
        return start_of_week(at)
    if period == "month":
        return start_of_month(at)
    # term: Ghanaian basic-school terms run roughly 13 weeks.
    return start_of_day(at) - timedelta(days=91)


def format_money(minor: int) -> str:
    negative = minor < 0
    abs_minor = abs(minor)
    whole = abs_minor // 100
    part = abs_minor % 100
    grouped = f"{whole:,}"
    sign = "-" if negative else ""
    return f"{sign}{CURRENCY_SYMBOL}{grouped}.{part:02d}"


def format_day_label(at: datetime) -> str:
    return DAY_LABELS[(at.weekday() + 1) % 7]  # Python Monday=0 -> JS Sunday=0 indexing


def format_short_date(at: datetime) -> str:
    return f"{at.day} {MONTH_LABELS[at.month - 1]}"


def delta_percent(current: int, previous: int) -> Optional[int]:
    if previous <= 0:
        return None
    return round(((current - previous) / previous) * 100)
