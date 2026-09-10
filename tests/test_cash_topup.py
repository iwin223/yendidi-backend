"""Integration tests for cash taken at the school office
(docs/BACKEND_HANDOVER.md §8a). Unlike every other wallet credit there is no
payment processor behind this one, so the server enforces everything the
client already does client-side: attribution from the bearer token, the
GH₵1-200 band, and a frozen-wallet refusal.

Same `TestClient` + `get_current_user` override pattern as
test_order_transitions.py, with its own dedicated NullPool engine for the
same cross-event-loop reason documented there.
"""
import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.models import (
    AuditLog,
    Parent,
    Role,
    School,
    SchoolStatus,
    Student,
    User,
    Wallet,
    WalletTransaction,
)
from app.main import app

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool, future=True)
TestSessionLocal = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


def run(coro):
    return asyncio.run(coro)


async def _seed():
    async with TestSessionLocal() as session:
        school = School(
            id=uuid4(), name="Test Cash Topup School", code=f"TCS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        other_school = School(
            id=uuid4(), name="Other Cash Topup School", code=f"OCS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        session.add(school)
        session.add(other_school)
        await session.flush()

        school_admin = User(id=uuid4(), role=Role.school_admin, full_name="Test School Admin", school_id=school.id, is_active=True)
        other_school_admin = User(id=uuid4(), role=Role.school_admin, full_name="Other School Admin", school_id=other_school.id, is_active=True)
        super_admin = User(id=uuid4(), role=Role.super_admin, full_name="Test Super Admin", is_active=True)
        parent_user = User(id=uuid4(), role=Role.parent, full_name="Test Parent", is_active=True)
        session.add(school_admin)
        session.add(other_school_admin)
        session.add(super_admin)
        session.add(parent_user)
        await session.flush()
        parent = Parent(id=uuid4(), user_id=parent_user.id, full_name="Test Parent", phone=f"+233{uuid4().int % 10**9:09d}")
        session.add(parent)

        student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TCS/{uuid4().int % 10000:04d}",
            first_name="Kid", last_name="Testerson", class_name="Basic 4", level="primary",
        )
        session.add(student)
        await session.flush()
        wallet = Wallet(id=uuid4(), student_id=student.id, balance_minor=1_000)
        session.add(wallet)

        frozen_student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TCS/{uuid4().int % 10000:04d}",
            first_name="Frozen", last_name="Wallet", class_name="Basic 3", level="primary",
        )
        session.add(frozen_student)
        await session.flush()
        frozen_wallet = Wallet(id=uuid4(), student_id=frozen_student.id, balance_minor=500, frozen=True)
        session.add(frozen_wallet)

        await session.commit()
        return {
            "school_id": school.id,
            "school_admin": school_admin,
            "other_school_admin": other_school_admin,
            "super_admin": super_admin,
            "parent_user": parent_user,
            "wallet_id": wallet.id,
            "student_id": student.id,
            "frozen_wallet_id": frozen_wallet.id,
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


def _wallet_balance(wallet_id) -> int:
    async def _get():
        async with TestSessionLocal() as session:
            wallet = await session.get(Wallet, wallet_id)
            return wallet.balance_minor

    return run(_get())


def test_school_admin_records_a_cash_topup(client, seeded):
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 5000, "note": "RCPT-42"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["balance_minor"] == balance_before + 5000
    assert _wallet_balance(seeded["wallet_id"]) == balance_before + 5000

    async def _check_rows():
        async with TestSessionLocal() as session:
            txn = (
                await session.execute(
                    select(WalletTransaction).where(WalletTransaction.id == body["transaction_id"])
                )
            ).scalar_one()
            assert txn.type == "topup"
            assert txn.method == "cash"
            assert txn.amount_minor == 5000
            assert txn.actor_id == seeded["school_admin"].id
            assert "RCPT-42" in txn.description

            audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == seeded["wallet_id"],
                        AuditLog.action == "wallet.cash_topup",
                    )
                )
            ).scalars().all()
            assert audit, "expected an audit log row for the cash top-up"

    run(_check_rows())


def test_super_admin_may_also_record_a_cash_topup(client, seeded):
    as_user(seeded["super_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 100},
    )
    assert resp.status_code == 200, resp.text


def test_parent_cannot_record_a_cash_topup(client, seeded):
    as_user(seeded["parent_user"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 100},
    )
    assert resp.status_code == 403


def test_a_school_admin_from_another_school_cannot_credit_this_wallet(client, seeded):
    as_user(seeded["other_school_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 100},
    )
    assert resp.status_code == 403


def test_amount_below_the_floor_is_rejected(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 50},
    )
    assert resp.status_code == 400


def test_amount_above_the_ceiling_is_rejected_not_clamped(client, seeded):
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['wallet_id']}/cash-topups",
        json={"amount_minor": 20_001},
    )
    assert resp.status_code == 400
    assert _wallet_balance(seeded["wallet_id"]) == balance_before


def test_a_frozen_wallet_refuses_cash(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/wallets/{seeded['frozen_wallet_id']}/cash-topups",
        json={"amount_minor": 100},
    )
    assert resp.status_code == 409


def test_unknown_wallet_is_404(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/wallets/{uuid4()}/cash-topups",
        json={"amount_minor": 100},
    )
    assert resp.status_code == 404
