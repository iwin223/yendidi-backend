"""Integration tests for face-template enrollment and its handoff into the
kiosk verify flow.

Same `TestClient` + `get_current_user` override pattern as
test_kiosk_and_guardian_links.py, with its own dedicated NullPool engine for
the same cross-event-loop reason documented there.
"""
import asyncio
import random
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
from app.db.models import FaceTemplate, Role, School, SchoolStatus, Student, User, Wallet
from app.main import app

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool, future=True)
TestSessionLocal = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

ARCFACE_DIM = 512


def run(coro):
    return asyncio.run(coro)


def _fake_embedding(seed: float = 0.1) -> list:
    # A constant-value vector (`[seed] * DIM`) is the wrong stand-in for a
    # real embedding here: any two positive constant vectors point in
    # exactly the same direction, so cosine similarity between them is 1.0
    # regardless of which scalar each uses — every "different" fake identity
    # would match every other one. A per-seed pseudo-random vector gives
    # same seed -> identical (for "this is a real match") and different
    # seed -> ~uncorrelated (for "this is not"), same as real embeddings.
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(ARCFACE_DIM)]


async def _seed():
    async with TestSessionLocal() as session:
        school = School(
            id=uuid4(), name="Test Face School", code=f"TFS-{uuid4().hex[:6]}",
            region="Test", district="Test", status=SchoolStatus.active,
        )
        session.add(school)
        await session.flush()

        super_admin = User(id=uuid4(), role=Role.super_admin, full_name="Super Admin", is_active=True)
        school_admin = User(id=uuid4(), role=Role.school_admin, full_name="School Admin", school_id=school.id, is_active=True)
        session.add(super_admin)
        session.add(school_admin)

        student = Student(
            id=uuid4(), user_id=None, school_id=school.id, student_code=f"TFS/{uuid4().int % 10000:04d}",
            first_name="Kid", last_name="Testerson", class_name="Basic 4", level="primary",
        )
        session.add(student)
        await session.flush()
        session.add(Wallet(id=uuid4(), student_id=student.id, balance_minor=5_000))

        await session.commit()
        return {
            "school_id": school.id,
            "super_admin": super_admin,
            "school_admin": school_admin,
            "student_id": student.id,
            "student_code": student.student_code,
        }


@pytest.fixture(scope="module")
def seeded():
    return run(_seed())


@pytest.fixture
def client():
    _rate_limit_buckets.clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def as_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _template_row(student_id):
    async def _get():
        async with TestSessionLocal() as session:
            result = await session.execute(select(FaceTemplate).where(FaceTemplate.student_id == student_id))
            return result.scalar_one_or_none()

    return run(_get())


def _pair_kiosk(client, seeded, label: str) -> str:
    as_user(seeded["super_admin"])
    create_resp = client.post(
        "/v1/kiosks",
        json={"school_id": str(seeded["school_id"]), "label": label, "exit_pin": "135791"},
    )
    assert create_resp.status_code == 201, create_resp.text
    pairing_code = create_resp.json()["pairing_code"]
    pair_resp = client.post("/v1/kiosks/pair", json={"pairing_code": pairing_code})
    assert pair_resp.status_code == 200, pair_resp.text
    return pair_resp.json()["device_token"]


# ---------------------------------------------------------------------------
# Enrollment: authorization and validation
# ---------------------------------------------------------------------------


def test_school_admin_can_enroll_a_face_for_their_own_student(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.put(
        f"/v1/students/{seeded['student_id']}/face-enrollment",
        json={"embedding": _fake_embedding(), "model_version": "arcface-r100-v1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["student_id"] == str(seeded["student_id"])
    assert body["model_version"] == "arcface-r100-v1"
    assert "embedding" not in body  # never echoed back


def test_enrollment_rejects_unsupported_model_version(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.put(
        f"/v1/students/{seeded['student_id']}/face-enrollment",
        json={"embedding": _fake_embedding(), "model_version": "some-future-model"},
    )
    assert resp.status_code == 400


def test_enrollment_rejects_wrong_length_embedding(client, seeded):
    as_user(seeded["school_admin"])
    resp = client.put(
        f"/v1/students/{seeded['student_id']}/face-enrollment",
        json={"embedding": [0.1, 0.2, 0.3], "model_version": "arcface-r100-v1"},
    )
    assert resp.status_code == 400


def test_other_schools_admin_cannot_enroll(client, seeded):
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
    resp = client.put(
        f"/v1/students/{seeded['student_id']}/face-enrollment",
        json={"embedding": _fake_embedding(), "model_version": "arcface-r100-v1"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Re-enrollment replaces rather than accumulates
# ---------------------------------------------------------------------------


def test_re_enrollment_replaces_the_existing_template(client, seeded):
    async def _fresh_student():
        async with TestSessionLocal() as session:
            student = Student(
                id=uuid4(), user_id=None, school_id=seeded["school_id"], student_code=f"TFS/{uuid4().int % 10000:04d}",
                first_name="Second", last_name="Kid", class_name="Basic 2", level="primary",
            )
            session.add(student)
            await session.flush()
            session.add(Wallet(id=uuid4(), student_id=student.id))
            await session.commit()
            return student.id

    student_id = run(_fresh_student())
    as_user(seeded["school_admin"])

    first = client.put(
        f"/v1/students/{student_id}/face-enrollment",
        json={"embedding": _fake_embedding(0.1), "model_version": "arcface-r100-v1"},
    )
    assert first.status_code == 200

    second = client.put(
        f"/v1/students/{student_id}/face-enrollment",
        json={"embedding": _fake_embedding(0.9), "model_version": "arcface-r100-v1"},
    )
    assert second.status_code == 200

    row = _template_row(student_id)
    assert row is not None
    assert row.embedding == _fake_embedding(0.9)  # replaced, not appended

    async def _count():
        async with TestSessionLocal() as session:
            result = await session.execute(select(FaceTemplate).where(FaceTemplate.student_id == student_id))
            return len(result.scalars().all())

    assert run(_count()) == 1


# ---------------------------------------------------------------------------
# Status and deletion never expose the embedding, and actually take effect
# ---------------------------------------------------------------------------


def test_status_reports_enrollment_without_ever_returning_the_embedding(client, seeded):
    async def _fresh_student():
        async with TestSessionLocal() as session:
            student = Student(
                id=uuid4(), user_id=None, school_id=seeded["school_id"], student_code=f"TFS/{uuid4().int % 10000:04d}",
                first_name="Third", last_name="Kid", class_name="Basic 3", level="primary",
            )
            session.add(student)
            await session.flush()
            session.add(Wallet(id=uuid4(), student_id=student.id))
            await session.commit()
            return student.id

    student_id = run(_fresh_student())
    as_user(seeded["school_admin"])

    before = client.get(f"/v1/students/{student_id}/face-enrollment")
    assert before.status_code == 200
    assert before.json()["enrolled"] is False

    client.put(
        f"/v1/students/{student_id}/face-enrollment",
        json={"embedding": _fake_embedding(), "model_version": "arcface-r100-v1"},
    )

    after = client.get(f"/v1/students/{student_id}/face-enrollment")
    assert after.status_code == 200
    body = after.json()
    assert body["enrolled"] is True
    assert body["model_version"] == "arcface-r100-v1"
    assert "embedding" not in body

    delete_resp = client.delete(f"/v1/students/{student_id}/face-enrollment")
    assert delete_resp.status_code == 204
    assert _template_row(student_id) is None

    gone = client.get(f"/v1/students/{student_id}/face-enrollment")
    assert gone.json()["enrolled"] is False

    # Deleting again (nothing left to delete) is a clean 404, not a crash.
    redundant_delete = client.delete(f"/v1/students/{student_id}/face-enrollment")
    assert redundant_delete.status_code == 404


# ---------------------------------------------------------------------------
# POST /kiosks/verify-face — matching happens server-side, never on the kiosk
# ---------------------------------------------------------------------------


def _enroll(client, seeded, student_id, embedding, model_version="arcface-r100-v1", as_admin=None):
    as_user(as_admin or seeded["school_admin"])
    resp = client.put(
        f"/v1/students/{student_id}/face-enrollment",
        json={"embedding": embedding, "model_version": model_version},
    )
    assert resp.status_code == 200, resp.text


def _new_student(seeded, first_name: str, balance_minor: int = 2_000, school_id=None):
    async def _create():
        async with TestSessionLocal() as session:
            student = Student(
                id=uuid4(), user_id=None, school_id=school_id or seeded["school_id"],
                student_code=f"TFS/{uuid4().int % 10000:04d}",
                first_name=first_name, last_name="Kid", class_name="Basic 4", level="primary",
            )
            session.add(student)
            await session.flush()
            session.add(Wallet(id=uuid4(), student_id=student.id, balance_minor=balance_minor))
            await session.commit()
            return student.id, student.student_code

    return run(_create())


def test_face_verify_matches_the_enrolled_pupil_by_similarity_alone(client, seeded):
    student_id, _ = _new_student(seeded, "Faceverify")
    _enroll(client, seeded, student_id, _fake_embedding(0.42))
    device_token = _pair_kiosk(client, seeded, "Face verify kiosk 1")

    # No student_code anywhere in this request — face is meant to identify
    # the pupil on its own, not confirm one already named.
    resp = client.post(
        "/v1/kiosks/verify-face",
        json={"embedding": _fake_embedding(0.42), "model_version": "arcface-r100-v1"},
        headers={"X-Kiosk-Token": device_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["student_id"] == str(student_id)
    assert "verification_token" in body
    # The response is the same shape `/kiosks/verify` returns — no face data
    # in it anywhere.
    assert "face_embedding" not in body


def test_face_verify_finds_no_match_when_nothing_is_close_enough(client, seeded):
    student_id, _ = _new_student(seeded, "Nomatch")
    _enroll(client, seeded, student_id, _fake_embedding(0.9))
    device_token = _pair_kiosk(client, seeded, "Face verify kiosk 2")

    # A wildly different embedding (opposite corner of the vector space)
    # must not match, and must fail exactly like an unknown student code —
    # a generic 404, not a distinguishable "close but no" response.
    resp = client.post(
        "/v1/kiosks/verify-face",
        json={"embedding": _fake_embedding(-0.9), "model_version": "arcface-r100-v1"},
        headers={"X-Kiosk-Token": device_token},
    )
    assert resp.status_code == 404


def test_face_verify_never_matches_a_pupil_enrolled_at_another_school(client, seeded):
    async def _other_school():
        async with TestSessionLocal() as session:
            other = School(id=uuid4(), name="Other Face School", code=f"OFS-{uuid4().hex[:6]}", region="x", district="x")
            session.add(other)
            await session.commit()
            return other.id

    other_school_id = run(_other_school())
    other_student_id, _ = _new_student(seeded, "Elsewhere", school_id=other_school_id)
    # A seed not reused by any other test in this module — this test needs
    # the *only* enrolled match anywhere to be the other-school pupil, or a
    # coincidental same-school hit would make a real scoping bug pass too.
    unique_embedding = _fake_embedding(0.271828)
    # Seeded via super_admin, not this school's admin — that admin correctly
    # cannot touch a pupil at a different school (test_other_schools_admin_
    # cannot_enroll already covers that authorization boundary); this test
    # is only about whether the *search* respects school scope.
    _enroll(client, seeded, other_student_id, unique_embedding, as_admin=seeded["super_admin"])

    device_token = _pair_kiosk(client, seeded, "Face verify kiosk 3")
    resp = client.post(
        "/v1/kiosks/verify-face",
        json={"embedding": unique_embedding, "model_version": "arcface-r100-v1"},
        headers={"X-Kiosk-Token": device_token},
    )
    # A perfect embedding match, but for a pupil at a different school — the
    # search is scoped to this kiosk's school and must not see it.
    assert resp.status_code == 404


def test_face_verify_ignores_templates_enrolled_under_a_different_model_version(client, seeded):
    student_id, _ = _new_student(seeded, "Oldmodel")
    _enroll(client, seeded, student_id, _fake_embedding(0.42), model_version="arcface-r100-v1")
    device_token = _pair_kiosk(client, seeded, "Face verify kiosk 4")

    # Two embeddings from different models are not comparable numbers, even
    # if they happen to line up — a mismatched model_version must never be
    # treated as a candidate.
    resp = client.post(
        "/v1/kiosks/verify-face",
        json={"embedding": _fake_embedding(0.42), "model_version": "some-future-model"},
        headers={"X-Kiosk-Token": device_token},
    )
    assert resp.status_code == 404


def test_face_verify_token_can_place_an_order_like_the_code_path(client, seeded):
    async def _seed_vendor_and_menu():
        from app.db.models import FoodCategory, MenuItem, Vendor, VendorStatus

        async with TestSessionLocal() as session:
            vendor_user = User(id=uuid4(), role=Role.vendor, full_name="Face Vendor", is_active=True)
            session.add(vendor_user)
            await session.flush()
            vendor = Vendor(
                id=uuid4(), user_id=vendor_user.id, business_name="Face Kitchen", owner_name="Owner",
                phone="+233200000097", status=VendorStatus.approved, accepting_orders=True,
                opens_at_minutes=0, closes_at_minutes=1439,
            )
            session.add(vendor)
            await session.flush()
            menu_item = MenuItem(
                id=uuid4(), vendor_id=vendor.id, name="Face Rice", category=FoodCategory.lunch,
                price_minor=400, art_key="test_rice", available=True, stock_count=50,
            )
            session.add(menu_item)
            await session.commit()
            return vendor.id, menu_item.id

    vendor_id, menu_item_id = run(_seed_vendor_and_menu())
    student_id, _ = _new_student(seeded, "Facepay", balance_minor=5_000)
    _enroll(client, seeded, student_id, _fake_embedding(0.42))
    device_token = _pair_kiosk(client, seeded, "Face verify kiosk 5")

    verify_resp = client.post(
        "/v1/kiosks/verify-face",
        json={"embedding": _fake_embedding(0.42), "model_version": "arcface-r100-v1"},
        headers={"X-Kiosk-Token": device_token},
    )
    assert verify_resp.status_code == 200
    token = verify_resp.json()["verification_token"]

    order_resp = client.post(
        "/v1/kiosks/orders",
        json={
            "vendor_id": str(vendor_id),
            "items": [{"menu_item_id": str(menu_item_id), "quantity": 1}],
            "pickup_slot": "12:00",
        },
        headers={"X-Kiosk-Token": device_token, "X-Verification-Token": token, "idempotency-key": str(uuid4())},
    )
    assert order_resp.status_code == 200, order_resp.text
