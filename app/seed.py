"""Create a small, repeatable development dataset."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select

from app.core.security import get_password_hash
from app.db.models import (
    Announcement,
    DeviceRegistration,
    FoodCategory,
    Guardianship,
    Invoice,
    InvoiceStatus,
    MenuItem,
    Notification,
    Order,
    OrderEvent,
    OrderLine,
    OrderStatus,
    Parent,
    Role,
    School,
    SchoolStatus,
    Student,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    TransactionType,
    User,
    Vendor,
    VendorCertification,
    VendorSchool,
    VendorStatus,
    Wallet,
    WalletTransaction,
)
from app.db.session import AsyncSessionLocal


PASSWORD = "Password123!"


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(School).where(School.code == "Y3N-001"))
        school = result.scalars().first()
        if school:
            print("Seed data already exists (school Y3N-001); nothing to do.")
            return

    
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        school = School(
            id=uuid4(),
            name="Y3ndidi Academy",
            code="Y3N-001",
            region="Greater Accra",
            district="La Nkwantanang-Madina",
            address="12 Learning Avenue, Accra",
            phone="+233200000001",
            email="admin@y3ndidi.example",
            headteacher="Ama Mensah",
            levels=["Primary", "JHS"],
            status=SchoolStatus.active,
        )
        session.add(school)
        await session.flush()

        school_admin = User(
            id=uuid4(),
            role=Role.school_admin,
            full_name="Ama Mensah",
            email="admin@y3ndidi.example",
            phone="+233200000001",
            password_hash=get_password_hash(PASSWORD),
            school_id=school.id,
        )
        parent_user = User(
            id=uuid4(),
            role=Role.parent,
            full_name="Kwame Owusu",
            email="parent@y3ndidi.example",
            phone="+233200000002",
            password_hash=get_password_hash(PASSWORD),
        )
        vendor_user = User(
            id=uuid4(),
            role=Role.vendor,
            full_name="Efua Boateng",
            email="vendor@y3ndidi.example",
            phone="+233200000003",
            password_hash=get_password_hash(PASSWORD),
        )
        student_user = User(
            id=uuid4(),
            role=Role.student,
            full_name="Nana Owusu",
            email="student@y3ndidi.example",
            phone="+233200000004",
            password_hash=get_password_hash(PASSWORD),
            school_id=school.id,
        )
        session.add_all([school_admin, parent_user, vendor_user, student_user])
        await session.flush()

        parent = Parent(
            id=uuid4(),
            user_id=parent_user.id,
            full_name="Kwame Owusu",
            phone="+233200000002",
            occupation="Accountant",
        )
        student = Student(
            id=uuid4(),
            user_id=student_user.id,
            school_id=school.id,
            student_code="Y3N-STU-001",
            first_name="Nana",
            last_name="Owusu",
            class_name="JHS 2A",
            level="JHS",
            allergies=["Peanuts"],
            dietary_notes="No peanuts; prefers mild meals.",
        )
        vendor = Vendor(
            id=uuid4(),
            user_id=vendor_user.id,
            business_name="Efua's Kitchen",
            owner_name="Efua Boateng",
            phone="+233200000003",
            email="vendor@y3ndidi.example",
            description="Fresh Ghanaian meals prepared for school communities.",
            status=VendorStatus.approved,
            rating=4.8,
            rating_count=24,
            opens_at_minutes=420,
            closes_at_minutes=960,
            accepting_orders=True,
        )
        session.add_all([parent, student, vendor])
        await session.flush()

        subscription = Subscription(
            id=uuid4(),
            school_id=school.id,
            plan=SubscriptionPlan.annual,
            status=SubscriptionStatus.active,
            started_at=now - timedelta(days=45),
            current_period_end=now + timedelta(days=320),
            amount_minor=240000,
            seats=500,
            auto_renew=True,
        )
        session.add(subscription)
        await session.flush()
        session.add(Invoice(
            id=uuid4(),
            subscription_id=subscription.id,
            amount_minor=240000,
            status=InvoiceStatus.paid,
            period_start=subscription.started_at,
            period_end=subscription.current_period_end,
            method="paystack",
            reference="INV-Y3N-001",
        ))

        session.add_all([
            Guardianship(parent_id=parent.id, student_id=student.id, is_primary=True),
            VendorSchool(vendor_id=vendor.id, school_id=school.id, approved_at=now - timedelta(days=30)),
            VendorCertification(
                id=uuid4(),
                vendor_id=vendor.id,
                name="Food Hygiene Certificate",
                authority="Accra Metropolitan Assembly",
                number="AMA-FH-2026-001",
                issued_at=now - timedelta(days=60),
                expires_at=now + timedelta(days=305),
                verified=True,
                verified_by=school_admin.id,
                verified_at=now - timedelta(days=30),
            ),
        ])

        rice = MenuItem(
            id=uuid4(),
            vendor_id=vendor.id,
            name="Jollof Rice with Chicken",
            description="Mild jollof rice served with grilled chicken and vegetables.",
            category=FoodCategory.lunch,
            tags=[FoodCategory.lunch, FoodCategory.local],
            price_minor=3500,
            art_key="jollof_chicken",
            ingredients=["Rice", "Tomato", "Chicken", "Carrot"],
            stock_count=40,
            low_stock_threshold=8,
            prep_minutes=15,
            kcal=620,
        )
        drink = MenuItem(
            id=uuid4(),
            vendor_id=vendor.id,
            name="Mango Juice",
            description="Chilled mango juice with no added caffeine.",
            category=FoodCategory.drinks,
            tags=[FoodCategory.drinks, FoodCategory.healthy],
            price_minor=1500,
            art_key="mango_juice",
            ingredients=["Mango", "Water"],
            stock_count=60,
            low_stock_threshold=10,
            prep_minutes=3,
            kcal=120,
        )
        session.add_all([rice, drink])
        await session.flush()

        wallet = Wallet(
            id=uuid4(),
            student_id=student.id,
            balance_minor=46500,
            daily_limit_minor=20000,
            weekly_limit_minor=80000,
            low_balance_threshold_minor=5000,
        )
        session.add(wallet)
        await session.flush()

        order = Order(
            id=uuid4(),
            code="Y3N-ORD-0001",
            student_id=student.id,
            school_id=school.id,
            vendor_id=vendor.id,
            subtotal_minor=6500,
            service_fee_minor=325,
            total_minor=6825,
            status=OrderStatus.ready,
            pickup_slot="12:30-13:00",
            note="Please keep the meal mild.",
            idempotency_key="seed-order-0001",
        )
        session.add(order)
        await session.flush()
        session.add_all([
            OrderLine(id=uuid4(), order_id=order.id, menu_item_id=rice.id, name=rice.name, art_key=rice.art_key, unit_price_minor=rice.price_minor, quantity=1),
            OrderLine(id=uuid4(), order_id=order.id, menu_item_id=drink.id, name=drink.name, art_key=drink.art_key, unit_price_minor=drink.price_minor, quantity=2),
            OrderEvent(id=uuid4(), order_id=order.id, status=OrderStatus.pending, actor_id=parent_user.id, note="Order placed"),
            OrderEvent(id=uuid4(), order_id=order.id, status=OrderStatus.ready, actor_id=vendor_user.id, note="Ready for pickup"),
            WalletTransaction(
                id=uuid4(),
                wallet_id=wallet.id,
                student_id=student.id,
                type=TransactionType.topup,
                amount_minor=50000,
                balance_after_minor=53325,
                description="Parent wallet top-up",
                method="paystack",
                reference="TOPUP-Y3N-0001",
                actor_id=parent_user.id,
            ),
            WalletTransaction(
                id=uuid4(),
                wallet_id=wallet.id,
                student_id=student.id,
                type=TransactionType.purchase,
                amount_minor=-6825,
                balance_after_minor=46500,
                description="Order Y3N-ORD-0001",
                method="wallet",
                reference="TXN-Y3N-0001",
                order_id=order.id,
                actor_id=student_user.id,
            ),
        ])
        session.add_all([
            Announcement(
                id=uuid4(),
                school_id=school.id,
                author_id=school_admin.id,
                title="Welcome to Y3ndidi Academy",
                body="School meals are now available for online ordering.",
                audience=["parents", "students"],
            ),
            Notification(
                id=uuid4(),
                user_id=parent_user.id,
                kind="order_ready",
                title="Order ready for pickup",
                body="Order Y3N-ORD-0001 is ready at Efua's Kitchen.",
                meta={"order_id": str(order.id)},
                channels={"push": True, "email": False},
            ),
            DeviceRegistration(
                id=uuid4(),
                user_id=parent_user.id,
                device_token="seed-device-token-parent-001",
                platform="android",
            ),
            DeviceRegistration(
                id=uuid4(),
                user_id=school_admin.id,
                device_token="seed-device-token-admin-001",
                platform="web",
            ),
            DeviceRegistration(
                id=uuid4(),
                user_id=student_user.id,
                device_token="seed-device-token-student-001",
                platform="ios",
            ),
            DeviceRegistration(
                id=uuid4(),
                user_id=vendor_user.id,
                device_token="seed-device-token-vendor-001",
                platform="android",
            ),
        ])
        await session.commit()
        print("Seeded Y3ndidi development data.")
        print(f"Login password for all demo users: {PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed())