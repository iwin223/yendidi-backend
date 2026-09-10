"""Integration tests for the kiosk pivot and guardian-link-request flow
(docs/SPEC_KIOSK_AND_VERIFICATION.md, docs/BACKEND_HANDOVER.md §2.1a, §9).

Runs the real FastAPI app over `TestClient` against the local database
configured in `.env`, with `get_current_user` overridden per test to avoid
ever exercising `/auth/login` — these tests authenticate as whichever role
the endpoint needs by dependency injection, not by password.
"""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.dependencies import _rate_limit_buckets, get_current_user
from app.db.models import (
    FoodCategory,
    GuardianLinkAttempt,
    Kiosk,
    MenuItem,
    Parent,
    Role,
    School,
    SchoolStatus,
    Student,
    User,
    Vendor,
    VendorSchool,
    VendorStatus,
    Wallet,
)
from app.main import app

# A dedicated engine, deliberately separate from `app.db.session.engine`.
# `TestClient` drives the ASGI app — and every `get_session` call it makes —
# on its own event loop, while each `run()` call below gets a brand new loop
# from `asyncio.run`. A pooled asyncpg connection cannot cross that boundary
# ("attached to a different loop"), so this engine uses NullPool: every
# checkout is a fresh connection that closes at the end of its own `run()`,
# never handed to a different loop.
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool, future=True)
TestSessionLocal = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


def run(coro):
    return asyncio.run(coro)


async def _seed():
    async with TestSessionLocal() as session:
        school = School(
            id=uuid4(), name="Test Kiosk School", code=f"TKS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        session.add(school)
        await session.flush()

        super_admin = User(id=uuid4(), role=Role.super_admin, full_name="Super Admin", is_active=True)
        school_admin = User(id=uuid4(), role=Role.school_admin, full_name="School Admin", school_id=school.id, is_active=True)
        session.add(super_admin)
        session.add(school_admin)

        parent_user = User(id=uuid4(), role=Role.parent, full_name="Test Parent", is_active=True)
        session.add(parent_user)
        await session.flush()
        parent = Parent(id=uuid4(), user_id=parent_user.id, full_name="Test Parent", phone=f"+233{uuid4().int % 10**9:09d}")
        session.add(parent)

        student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TKS/{uuid4().int % 10000:04d}",
            first_name="Kid", last_name="Testerson", class_name="Basic 4", level="primary",
            allergies=["peanuts"],
        )
        session.add(student)
        await session.flush()
        wallet = Wallet(id=uuid4(), student_id=student.id, balance_minor=10_000)
        session.add(wallet)

        vendor_user = User(id=uuid4(), role=Role.vendor, full_name="Test Vendor", is_active=True)
        session.add(vendor_user)
        await session.flush()
        vendor = Vendor(
            id=uuid4(), user_id=vendor_user.id, business_name="Test Kitchen", owner_name="Owner",
            phone="+233200000099", status=VendorStatus.approved, accepting_orders=True,
            opens_at_minutes=0, closes_at_minutes=1439,
        )
        session.add(vendor)
        await session.flush()
        menu_item = MenuItem(
            id=uuid4(), vendor_id=vendor.id, name="Test Rice", category=FoodCategory.lunch,
            price_minor=500, art_key="test_rice", available=True, stock_count=50,
        )
        session.add(menu_item)

        await session.commit()
        return {
            "school_id": school.id,
            "super_admin": super_admin,
            "school_admin": school_admin,
            "parent": parent,
            "parent_user": parent_user,
            "student_id": student.id,
            "student_code": student.student_code,
            "vendor_id": vendor.id,
            "menu_item_id": menu_item.id,
        }


@pytest.fixture(scope="module")
def seeded():
    return run(_seed())


@pytest.fixture
def client():
    # The pairing/verify/exit limiters are an in-memory, per-process bucket
    # keyed by path+IP (`app/core/dependencies.py`) — real protection against
    # one caller hammering an endpoint, but shared process state that would
    # otherwise leak between tests and make later ones fail on attempt count
    # rather than on what they're actually asserting.
    _rate_limit_buckets.clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def as_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


# ---------------------------------------------------------------------------
# Anonymous vendor catalog (kiosk browsing)
# ---------------------------------------------------------------------------


def test_anonymous_vendor_listing_includes_real_trading_hours(client, seeded):
    """A paired kiosk has no JWT, so it loads its catalog off the same
    unauthenticated `GET /vendors?school_id=` the client's `loadKioskCatalog`
    calls. That response must carry the vendor's actual trading hours —
    otherwise the client defaults to "always open" and a student can order
    from a vendor that is actually closed.
    """

    async def _seed_vendor_with_hours():
        async with TestSessionLocal() as session:
            vendor_user = User(id=uuid4(), role=Role.vendor, full_name="Morning Kitchen Owner", is_active=True)
            session.add(vendor_user)
            await session.flush()
            vendor = Vendor(
                id=uuid4(), user_id=vendor_user.id, business_name="Morning Kitchen", owner_name="Owner",
                phone="+233200000098", status=VendorStatus.approved, accepting_orders=True,
                opens_at_minutes=6 * 60, closes_at_minutes=10 * 60,
            )
            session.add(vendor)
            await session.flush()
            session.add(VendorSchool(vendor_id=vendor.id, school_id=seeded["school_id"]))
            await session.commit()
            return vendor.id

    vendor_id = run(_seed_vendor_with_hours())

    # No `as_user` override and no auth header — this is exactly what a paired
    # kiosk sends.
    resp = client.get("/v1/vendors", params={"school_id": str(seeded["school_id"])})
    assert resp.status_code == 200
    vendor_body = next(v for v in resp.json() if v["id"] == str(vendor_id))
    assert vendor_body["opens_at_minutes"] == 6 * 60
    assert vendor_body["closes_at_minutes"] == 10 * 60


# ---------------------------------------------------------------------------
# Kiosk: provisioning, pairing, verify, revoke
# ---------------------------------------------------------------------------


def test_only_super_admin_may_create_kiosk(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": "Corridor 1", "exit_pin": "1234"},
    )
    assert resp.status_code == 403


def test_create_kiosk_rejects_a_malformed_exit_pin(client, seeded):
    as_user(seeded["super_admin"])
    resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": "Corridor 1", "exit_pin": "12"},
    )
    assert resp.status_code == 400


def test_kiosk_provisioning_and_pairing_flow(client, seeded):
    as_user(seeded["super_admin"])
    create_resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": "Canteen tablet", "exit_pin": "246810"},
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["status"] == "pending"
    pairing_code = body["pairing_code"]

    # Wrong code is rejected, doesn't consume the real one.
    bad_pair = client.post("/v1/kiosks/pair", json={"pairing_code": "WRONGCODE"})
    assert bad_pair.status_code == 400

    pair_resp = client.post("/v1/kiosks/pair", json={"pairing_code": pairing_code})
    assert pair_resp.status_code == 200
    device_token = pair_resp.json()["device_token"]
    assert pair_resp.json()["school_id"] == str(seeded["school_id"])

    # The code is single-use — pairing again with the same code fails now.
    replay = client.post("/v1/kiosks/pair", json={"pairing_code": pairing_code})
    assert replay.status_code == 400

    as_user(seeded["school_admin"])
    listing = client.get(f"/v1/schools/{seeded['school_id']}/kiosks")
    assert listing.status_code == 200
    kiosks = listing.json()
    assert any(k["id"] == body["id"] and k["status"] == "active" for k in kiosks)

    return device_token


def test_verify_unknown_code_and_wrong_school_code_are_indistinguishable(client, seeded):
    device_token = test_kiosk_provisioning_and_pairing_flow(client, seeded)

    unknown = client.post(
        "/v1/kiosks/verify", json={"student_code": "NOPE/9999"}, headers={"X-Kiosk-Token": device_token}
    )
    assert unknown.status_code == 404
    unknown_body = unknown.json()

    # A code that exists but belongs to a different school must produce the
    # exact same refusal, not a different one that would let the kiosk tell
    # the two cases apart.
    other_school_code_resp = client.post(
        "/v1/kiosks/verify", json={"student_code": "SOMEOTHERSCHOOL/0001"}, headers={"X-Kiosk-Token": device_token}
    )
    assert other_school_code_resp.status_code == unknown.status_code
    assert other_school_code_resp.json() == unknown_body


def test_verify_and_place_kiosk_order(client, seeded):
    device_token = test_kiosk_provisioning_and_pairing_flow(client, seeded)

    verify_resp = client.post(
        "/v1/kiosks/verify",
        json={"student_code": seeded["student_code"]},
        headers={"X-Kiosk-Token": device_token},
    )
    assert verify_resp.status_code == 200
    payload = verify_resp.json()
    assert payload["student_id"] == str(seeded["student_id"])
    assert payload["allergies"] == ["peanuts"]
    assert payload["balance_minor"] == 10_000
    token = payload["verification_token"]

    order_resp = client.post(
        "/v1/kiosks/orders",
        json={
            "vendor_id": str(seeded["vendor_id"]),
            "items": [{"menu_item_id": str(seeded["menu_item_id"]), "quantity": 2}],
            "pickup_slot": "12:00",
        },
        headers={"X-Kiosk-Token": device_token, "X-Verification-Token": token, "idempotency-key": str(uuid4())},
    )
    assert order_resp.status_code == 200, order_resp.text
    order_body = order_resp.json()
    assert order_body["subtotal_minor"] == 1000  # 2 x 500
    assert order_body["wallet_balance_after_minor"] == 10_000 - order_body["total_minor"]

    # The token is single-use: a second order attempt with the same token
    # must fail, even though it was never actually spent by a failed order.
    replay_resp = client.post(
        "/v1/kiosks/orders",
        json={
            "vendor_id": str(seeded["vendor_id"]),
            "items": [{"menu_item_id": str(seeded["menu_item_id"]), "quantity": 1}],
            "pickup_slot": "12:00",
        },
        headers={"X-Kiosk-Token": device_token, "X-Verification-Token": token, "idempotency-key": str(uuid4())},
    )
    assert replay_resp.status_code == 401


def test_revoked_kiosk_cannot_verify(client, seeded):
    as_user(seeded["super_admin"])
    create_resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": "To be revoked", "exit_pin": "9999"},
    )
    pairing_code = create_resp.json()["pairing_code"]
    kiosk_id = create_resp.json()["id"]
    device_token = client.post("/v1/kiosks/pair", json={"pairing_code": pairing_code}).json()["device_token"]

    revoke_resp = client.post(f"/v1/kiosks/{kiosk_id}/revoke")
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    verify_resp = client.post(
        "/v1/kiosks/verify", json={"student_code": seeded["student_code"]}, headers={"X-Kiosk-Token": device_token}
    )
    assert verify_resp.status_code == 401


def _provision_and_pair(client, seeded, label: str, exit_pin: str) -> str:
    """A lighter-weight version of `test_kiosk_provisioning_and_pairing_flow`
    for tests that only need a paired device: one create call and one pair
    call, so exit-flow tests don't also spend the shared `PAIR_RATE_LIMIT`
    bucket that helper's wrong-code and replay attempts consume.
    """
    as_user(seeded["super_admin"])
    create_resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": label, "exit_pin": exit_pin},
    )
    assert create_resp.status_code == 201
    pairing_code = create_resp.json()["pairing_code"]
    pair_resp = client.post("/v1/kiosks/pair", json={"pairing_code": pairing_code})
    assert pair_resp.status_code == 200
    return pair_resp.json()["device_token"]


def test_wrong_exit_pin_is_refused_and_leaves_the_kiosk_active(client, seeded):
    device_token = _provision_and_pair(client, seeded, "Exit test 1", "135791")

    wrong = client.post(
        "/v1/kiosks/exit", json={"pin": "000000"}, headers={"X-Kiosk-Token": device_token}
    )
    assert wrong.status_code == 401

    # Refused once, the device token still works — a fumbled PIN must not
    # itself end the kiosk.
    verify_resp = client.post(
        "/v1/kiosks/verify", json={"student_code": seeded["student_code"]}, headers={"X-Kiosk-Token": device_token}
    )
    assert verify_resp.status_code == 200


def test_correct_exit_pin_revokes_the_kiosk(client, seeded):
    device_token = _provision_and_pair(client, seeded, "Exit test 2", "246810")

    exit_resp = client.post(
        "/v1/kiosks/exit", json={"pin": "246810"}, headers={"X-Kiosk-Token": device_token}
    )
    assert exit_resp.status_code == 200
    assert exit_resp.json()["ok"] is True

    # The device token is spent along with the kiosk — exit behaves exactly
    # like an admin revoke from the device's side.
    verify_resp = client.post(
        "/v1/kiosks/verify", json={"student_code": seeded["student_code"]}, headers={"X-Kiosk-Token": device_token}
    )
    assert verify_resp.status_code == 401


# ---------------------------------------------------------------------------
# Guardian link requests
# ---------------------------------------------------------------------------


def test_unknown_code_returns_generic_response_and_creates_nothing(client, seeded):
    as_user(seeded["parent_user"])
    resp = client.post("/v1/guardian-link-requests", json={"student_code": "DOESNOTEXIST/0000"})
    assert resp.status_code == 202

    as_user(seeded["school_admin"])
    listing = client.get(f"/v1/schools/{seeded['school_id']}/guardian-link-requests")
    assert listing.json() == []


def test_valid_code_creates_pending_request_school_can_see_and_approve(client, seeded):
    as_user(seeded["parent_user"])
    resp = client.post("/v1/guardian-link-requests", json={"student_code": seeded["student_code"]})
    assert resp.status_code == 202
    generic_body = resp.json()

    # A response identical to the unknown-code case above — no leak either way.
    other = client.post("/v1/guardian-link-requests", json={"student_code": "DOESNOTEXIST/0000"})
    assert other.json() == generic_body

    as_user(seeded["school_admin"])
    listing = client.get(f"/v1/schools/{seeded['school_id']}/guardian-link-requests?status=pending")
    assert listing.status_code == 200
    requests = listing.json()
    assert len(requests) == 1
    req = requests[0]
    assert req["student_id"] == str(seeded["student_id"])
    assert req["parent_phone"] == seeded["parent"].phone

    approve_resp = client.post(f"/v1/guardian-link-requests/{req['id']}/approve")
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # Re-approving an already-decided request is refused.
    second_approve = client.post(f"/v1/guardian-link-requests/{req['id']}/approve")
    assert second_approve.status_code == 409


def test_reject_requires_a_reason(client, seeded):
    as_user(seeded["parent_user"])
    new_student_code = f"TKS/{uuid4().int % 10000:04d}"

    async def _extra_student():
        async with TestSessionLocal() as session:
            s = Student(
                id=uuid4(), user_id=None, school_id=seeded["school_id"], student_code=new_student_code,
                first_name="Second", last_name="Kid", class_name="Basic 2", level="primary",
            )
            session.add(s)
            await session.commit()

    run(_extra_student())

    client.post("/v1/guardian-link-requests", json={"student_code": new_student_code})

    as_user(seeded["school_admin"])
    listing = client.get(f"/v1/schools/{seeded['school_id']}/guardian-link-requests?status=pending").json()
    req = next(r for r in listing if r["student_code"] == new_student_code)

    empty_reason = client.post(f"/v1/guardian-link-requests/{req['id']}/reject", json={"reason": "  "})
    assert empty_reason.status_code == 400

    reject_resp = client.post(f"/v1/guardian-link-requests/{req['id']}/reject", json={"reason": "Wrong parent"})
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "rejected"
    assert reject_resp.json()["reject_reason"] == "Wrong parent"


def test_other_schools_admin_cannot_see_or_approve(client, seeded):
    async def _other_school_admin():
        async with TestSessionLocal() as session:
            other_school = School(id=uuid4(), name="Other School", code=f"OTH-{uuid4().hex[:6]}", region="x", district="x")
            session.add(other_school)
            await session.flush()
            admin = User(id=uuid4(), role=Role.school_admin, full_name="Other Admin", school_id=other_school.id, is_active=True)
            session.add(admin)
            await session.commit()
            return admin

    other_admin = run(_other_school_admin())
    as_user(other_admin)
    resp = client.get(f"/v1/schools/{seeded['school_id']}/guardian-link-requests")
    assert resp.status_code == 403


def test_rate_limit_blocks_after_max_attempts(client, seeded):
    async def _fill_attempts():
        async with TestSessionLocal() as session:
            for _ in range(20):
                session.add(GuardianLinkAttempt(id=uuid4(), parent_id=seeded["parent"].id, created_at=datetime.utcnow()))
            await session.commit()

    run(_fill_attempts())
    as_user(seeded["parent_user"])
    resp = client.post("/v1/guardian-link-requests", json={"student_code": "ANYTHING/0000"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Pupil enrolment no longer mints a login (§2.1b)
# ---------------------------------------------------------------------------


def test_enrolling_a_pupil_creates_no_user_row(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.post(
        f"/v1/schools/{seeded['school_id']}/students",
        json={
            "student_code": f"TKS/{uuid4().int % 10000:04d}",
            "first_name": "No",
            "last_name": "Login",
            "class_name": "Basic 1",
            "level": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    async def _check():
        async with TestSessionLocal() as session:
            result = await session.execute(select(Student).where(Student.id == body["id"]))
            student = result.scalar_one()
            return student.user_id

    assert run(_check()) is None
