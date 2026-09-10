"""Integration tests for the two-outcome order-status model: a vendor either
confirms a paid order or cancels it (refunding the wallet in full). There is
no student-initiated cancel and no intermediate kitchen-stage tracking.

Same `TestClient` + `get_current_user` override pattern as
test_kiosk_and_guardian_links.py, with its own dedicated NullPool engine for
the same cross-event-loop reason documented there.
"""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.models import (
    FoodCategory,
    MenuItem,
    Order,
    OrderStatus,
    Role,
    School,
    SchoolStatus,
    Student,
    User,
    Vendor,
    VendorStatus,
    Wallet,
)
from app.main import app

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool, future=True)
TestSessionLocal = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


def run(coro):
    return asyncio.run(coro)


async def _seed():
    async with TestSessionLocal() as session:
        school = School(
            id=uuid4(), name="Test Transitions School", code=f"TTS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        session.add(school)
        await session.flush()

        student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TTS/{uuid4().int % 10000:04d}",
            first_name="Kid", last_name="Testerson", class_name="Basic 4", level="primary",
        )
        session.add(student)
        await session.flush()
        wallet = Wallet(id=uuid4(), student_id=student.id, balance_minor=5_000)
        session.add(wallet)

        vendor_user = User(id=uuid4(), role=Role.vendor, full_name="Test Vendor", is_active=True)
        other_vendor_user = User(id=uuid4(), role=Role.vendor, full_name="Other Vendor", is_active=True)
        session.add(vendor_user)
        session.add(other_vendor_user)
        await session.flush()
        vendor = Vendor(
            id=uuid4(), user_id=vendor_user.id, business_name="Test Kitchen", owner_name="Owner",
            phone="+233200000098", status=VendorStatus.approved, accepting_orders=True,
            opens_at_minutes=0, closes_at_minutes=1439,
        )
        other_vendor = Vendor(
            id=uuid4(), user_id=other_vendor_user.id, business_name="Other Kitchen", owner_name="Owner",
            phone="+233200000097", status=VendorStatus.approved, accepting_orders=True,
            opens_at_minutes=0, closes_at_minutes=1439,
        )
        session.add(vendor)
        session.add(other_vendor)
        await session.flush()
        menu_item = MenuItem(
            id=uuid4(), vendor_id=vendor.id, name="Test Rice", category=FoodCategory.lunch,
            price_minor=1000, art_key="test_rice", available=True, stock_count=50,
        )
        session.add(menu_item)

        parent_user = User(id=uuid4(), role=Role.parent, full_name="Test Parent", is_active=True)
        session.add(parent_user)

        await session.commit()
        return {
            "student_id": student.id,
            "wallet_id": wallet.id,
            "vendor_user": vendor_user,
            "vendor_id": vendor.id,
            "other_vendor_user": other_vendor_user,
            "parent_user": parent_user,
        }


@pytest.fixture(scope="module")
def seeded():
    return run(_seed())


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def as_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


async def _create_paid_order(seeded, total_minor: int = 1050) -> str:
    async with TestSessionLocal() as session:
        # `school_id` is a required FK not carried on `seeded` directly —
        # fetch it off the seeded student instead of threading another value
        # through the helper's signature.
        student = await session.get(Student, seeded["student_id"])
        order = Order(
            id=uuid4(),
            code=f"ORD-TEST-{uuid4().hex[:8]}",
            student_id=seeded["student_id"],
            school_id=student.school_id,
            vendor_id=seeded["vendor_id"],
            subtotal_minor=1000,
            service_fee_minor=50,
            total_minor=total_minor,
            status=OrderStatus.paid,
            pickup_slot="12:00",
            placed_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(order)
        await session.commit()
        return str(order.id)


def _wallet_balance(wallet_id) -> int:
    async def _get():
        async with TestSessionLocal() as session:
            wallet = await session.get(Wallet, wallet_id)
            return wallet.balance_minor

    return run(_get())


def test_vendor_confirms_a_paid_order(client, seeded):
    order_id = run(_create_paid_order(seeded))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["vendor_user"])
    resp = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "confirmed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    # Confirming moves no money — only cancelling does.
    assert _wallet_balance(seeded["wallet_id"]) == balance_before


def test_vendor_cancels_a_paid_order_and_wallet_is_refunded(client, seeded):
    order_id = run(_create_paid_order(seeded, total_minor=750))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["vendor_user"])
    resp = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "cancelled"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    assert _wallet_balance(seeded["wallet_id"]) == balance_before + 750


def test_a_different_vendor_cannot_transition_the_order(client, seeded):
    order_id = run(_create_paid_order(seeded))

    as_user(seeded["other_vendor_user"])
    resp = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "confirmed"})
    assert resp.status_code == 403


def test_non_vendor_roles_cannot_transition_the_order(client, seeded):
    order_id = run(_create_paid_order(seeded))

    as_user(seeded["parent_user"])
    resp = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "confirmed"})
    assert resp.status_code == 403


def test_a_terminal_order_cannot_be_transitioned_again(client, seeded):
    order_id = run(_create_paid_order(seeded))

    as_user(seeded["vendor_user"])
    first = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "confirmed"})
    assert first.status_code == 200

    second = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "cancelled"})
    assert second.status_code == 400


def test_new_status_must_be_confirmed_or_cancelled(client, seeded):
    order_id = run(_create_paid_order(seeded))

    as_user(seeded["vendor_user"])
    resp = client.post(f"/v1/orders/{order_id}/transition", json={"new_status": "paid"})
    assert resp.status_code == 400
