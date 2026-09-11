"""Integration tests for the Paystack topup-status reconciliation fallback.

The webhook is fire-and-forget from Paystack's side and has nowhere to be
delivered against a developer's own machine — and even in production can be
delayed or dropped. `GET /topups/{id}` must be able to notice a still-pending
topup actually succeeded (or failed) by asking Paystack directly, not just
echo back whatever the DB row says, which a webhook that never arrives would
leave at "pending" forever.

Same `TestClient` + `get_current_user` override pattern as
test_order_transitions.py, with its own dedicated NullPool engine for the
same cross-event-loop reason documented there.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.models import Parent, Role, School, SchoolStatus, Student, Topup, TopupStatus, User, Wallet
from app.main import app
from app.payments import PaystackError

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool, future=True)
TestSessionLocal = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


def run(coro):
    return asyncio.run(coro)


async def _seed():
    async with TestSessionLocal() as session:
        school = School(
            id=uuid4(), name="Test Topup School", code=f"TTS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        session.add(school)
        await session.flush()

        parent_user = User(id=uuid4(), role=Role.parent, full_name="Test Parent", is_active=True)
        session.add(parent_user)
        await session.flush()
        parent = Parent(id=uuid4(), user_id=parent_user.id, full_name="Test Parent", phone=f"+233{uuid4().int % 10**9:09d}")
        session.add(parent)

        student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TTS/{uuid4().int % 10000:04d}",
            first_name="Kid", last_name="Testerson", class_name="Basic 4", level="primary",
        )
        session.add(student)
        await session.flush()
        wallet = Wallet(id=uuid4(), student_id=student.id, balance_minor=5_000)
        session.add(wallet)
        await session.flush()

        from app.db.models import Guardianship
        session.add(Guardianship(parent_id=parent.id, student_id=student.id))

        await session.commit()
        return {"parent_user": parent_user, "wallet_id": wallet.id}


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


async def _create_pending_topup(seeded, amount_minor: int = 3_000) -> str:
    async with TestSessionLocal() as session:
        topup = Topup(
            id=uuid4(),
            wallet_id=seeded["wallet_id"],
            initiated_by=seeded["parent_user"].id,
            amount_minor=amount_minor,
            method="mtn_momo",
            processor="paystack",
            idempotency_key=str(uuid4()),
            status=TopupStatus.pending,
            created_at=datetime.utcnow(),
        )
        session.add(topup)
        await session.commit()
        return str(topup.id)


def _wallet_balance(wallet_id) -> int:
    async def _get():
        async with TestSessionLocal() as session:
            wallet = await session.get(Wallet, wallet_id)
            return wallet.balance_minor

    return run(_get())


def test_polling_a_pending_topup_reconciles_success(client, seeded):
    topup_id = run(_create_pending_topup(seeded, amount_minor=3_000))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["parent_user"])
    with patch("app.api.wallet.verify_paystack_transaction", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = {"status": "success", "reference": topup_id}
        resp = client.get(f"/v1/topups/{topup_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["settled_at"] is not None
    assert _wallet_balance(seeded["wallet_id"]) == balance_before + 3_000


def test_polling_a_failed_topup_marks_it_failed_without_crediting(client, seeded):
    topup_id = run(_create_pending_topup(seeded, amount_minor=1_500))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["parent_user"])
    with patch("app.api.wallet.verify_paystack_transaction", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = {"status": "failed", "gateway_response": "Insufficient funds"}
        resp = client.get(f"/v1/topups/{topup_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "Insufficient funds"
    assert _wallet_balance(seeded["wallet_id"]) == balance_before


def test_polling_leaves_a_topup_pending_when_paystack_has_no_answer_yet(client, seeded):
    topup_id = run(_create_pending_topup(seeded, amount_minor=2_000))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["parent_user"])
    with patch("app.api.wallet.verify_paystack_transaction", new_callable=AsyncMock) as mock_verify:
        mock_verify.side_effect = PaystackError("Transaction reference not found.")
        resp = client.get(f"/v1/topups/{topup_id}")

    # A verify failure (Paystack unreachable, or a race between initialize and
    # verify) is not this request's problem — the client just polls again.
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert _wallet_balance(seeded["wallet_id"]) == balance_before


def test_polling_an_abandoned_checkout_leaves_it_pending_without_crediting(client, seeded):
    # "abandoned" is Paystack's default verify answer before the payer has
    # done anything — the state almost every poll sees while the in-app
    # checkout is still open, not a statement that they gave up. Treating it
    # as terminal failed the topup out from under a payer who was still
    # looking at the card form.
    topup_id = run(_create_pending_topup(seeded, amount_minor=1_200))
    balance_before = _wallet_balance(seeded["wallet_id"])

    as_user(seeded["parent_user"])
    with patch("app.api.wallet.verify_paystack_transaction", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = {"status": "abandoned"}
        resp = client.get(f"/v1/topups/{topup_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert _wallet_balance(seeded["wallet_id"]) == balance_before


def test_an_already_succeeded_topup_is_not_re_verified(client, seeded):
    topup_id = run(_create_pending_topup(seeded, amount_minor=1_000))

    as_user(seeded["parent_user"])
    with patch("app.api.wallet.verify_paystack_transaction", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = {"status": "success", "reference": topup_id}
        first = client.get(f"/v1/topups/{topup_id}")
        assert first.json()["status"] == "succeeded"
        balance_after_first = _wallet_balance(seeded["wallet_id"])

        # A second poll must not call Paystack again, and must not credit twice.
        second = client.get(f"/v1/topups/{topup_id}")
        assert second.json()["status"] == "succeeded"
        assert _wallet_balance(seeded["wallet_id"]) == balance_after_first
        assert mock_verify.call_count == 1
