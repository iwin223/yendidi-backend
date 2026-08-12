from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4,UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.dependencies import get_current_user, get_session
from app.db.models import (
    Guardianship,
    MenuItem,
    Order,
    OrderEvent,
    OrderLine,
    OrderStatus,
    Parent,
    Role,
    Student,
    User,
    Vendor,
    VendorStatus,
    Wallet,
    WalletTransaction,
)
from app.db.session import AsyncSession

router = APIRouter()


class OrderItemRequest(BaseModel):
    menu_item_id: str
    quantity: int


class OrderPreviewRequest(BaseModel):
    vendor_id: str
    items: List[OrderItemRequest]
    pickup_slot: str
    note: Optional[str] = None


class OrderPreviewResponse(BaseModel):
    subtotal_minor: int
    service_fee_minor: int
    total_minor: int
    estimated_ready_in_minutes: int


class OrderCreateResponse(BaseModel):
    id: UUID
    code: str
    status: OrderStatus
    subtotal_minor: int
    service_fee_minor: int
    total_minor: int
    wallet_balance_after_minor: int
    placed_at: datetime

    class Config:
        from_attributes = True


class OrderLineResponse(BaseModel):
    id: UUID
    menu_item_id: Optional[UUID]
    name: str
    art_key: str
    unit_price_minor: int
    quantity: int

    class Config:
        from_attributes = True


class OrderDetailResponse(BaseModel):
    id: UUID
    code: str
    student_id: UUID
    school_id: UUID
    vendor_id: UUID
    subtotal_minor: int
    service_fee_minor: int
    total_minor: int
    status: OrderStatus
    pickup_slot: str
    note: Optional[str]
    placed_at: datetime
    updated_at: datetime
    lines: List[OrderLineResponse]

    class Config:
        from_attributes = True


class OrderSummaryResponse(BaseModel):
    id: UUID
    code: str
    vendor_id: UUID
    total_minor: int
    status: OrderStatus
    placed_at: datetime

    class Config:
        from_attributes = True


class OrderTransitionRequest(BaseModel):
    new_status: OrderStatus
    note: Optional[str] = None


SERVICE_FEE_PERCENT = 5
ESTIMATED_PREP_MINUTES = 15
ALLOWED_TRANSITIONS = {
    OrderStatus.pending,
    OrderStatus.accepted,
    OrderStatus.preparing,
    OrderStatus.ready,
    OrderStatus.completed,
    OrderStatus.rejected,
    OrderStatus.cancelled,
}


def compute_order_totals(items: List[MenuItem], quantities: List[int]) -> tuple[int, int, int]:
    subtotal = sum(item.price_minor * quantity for item, quantity in zip(items, quantities))
    service_fee = max(0, (subtotal * SERVICE_FEE_PERCENT) // 100)
    return subtotal, service_fee, subtotal + service_fee


async def _sum_purchase_spend(student_id: UUID, since: datetime, session: AsyncSession) -> int:
    stmt = select(WalletTransaction.amount_minor).where(
        WalletTransaction.student_id == student_id,
        WalletTransaction.created_at >= since,
    )
    result = await session.execute(stmt)
    amounts = result.scalars().all()
    return abs(sum(amount for amount in amounts if amount < 0))


async def authorize_order_access(order: Order, current_user: User, session: AsyncSession) -> None:
    if current_user.role == "student":
        student_stmt = select(Student).where(Student.user_id == current_user.id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.id != order.student_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")
        return

    if current_user.role == "vendor":
        vendor_stmt = select(Vendor).where(Vendor.user_id == current_user.id)
        vendor_result = await session.execute(vendor_stmt)
        vendor = vendor_result.scalar_one_or_none()
        if not vendor or vendor.id != order.vendor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")
        return

    if current_user.role == "school_admin" and current_user.school_id == order.school_id:
        return

    if current_user.role == "super_admin":
        return

    if current_user.role == "parent":
        parent_stmt = select(Parent).where(Parent.user_id == current_user.id)
        parent_result = await session.execute(parent_stmt)
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")
        guard_stmt = select(Guardianship).where(Guardianship.parent_id == parent.id, Guardianship.student_id == order.student_id)
        guard_result = await session.execute(guard_stmt)
        if guard_result.scalar_one_or_none():
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")


@router.post("/orders/preview", response_model=OrderPreviewResponse)
async def orders_preview(request: OrderPreviewRequest, session: AsyncSession = Depends(get_session)):
    if not request.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order must include at least one item")

    vendor_stmt = select(Vendor).where(Vendor.id == request.vendor_id)
    vendor_result = await session.execute(vendor_stmt)
    vendor = vendor_result.scalar_one_or_none()
    if not vendor or vendor.status != VendorStatus.approved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found or not approved")
    if not vendor.accepting_orders:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vendor is not accepting orders")

    current_minute = datetime.utcnow().hour * 60 + datetime.utcnow().minute
    if vendor.opens_at_minutes is not None and vendor.closes_at_minutes is not None:
        if vendor.opens_at_minutes <= vendor.closes_at_minutes:
            open_window = vendor.opens_at_minutes <= current_minute < vendor.closes_at_minutes
        else:
            open_window = current_minute >= vendor.opens_at_minutes or current_minute < vendor.closes_at_minutes
        if not open_window:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vendor is currently closed")

    menu_item_ids = [item.menu_item_id for item in request.items]
    statement = select(MenuItem).where(MenuItem.id.in_(menu_item_ids))
    result = await session.execute(statement)
    menu_items = result.scalars().all()

    if len(menu_items) != len(menu_item_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more menu items are invalid")
    # `request.vendor_id` is a raw str while `item.vendor_id` is a UUID once loaded from
    # the DB — comparing them directly is always unequal, so every preview was rejected
    # regardless of whether the item actually belonged to the vendor. Compare against the
    # already-fetched `vendor.id` (a real UUID) instead, matching place_order()'s check.
    if any(item.vendor_id != vendor.id for item in menu_items):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more menu items are not sold by the selected vendor")

    quantities = [item.quantity for item in request.items]
    subtotal, service_fee, total = compute_order_totals(menu_items, quantities)

    return OrderPreviewResponse(
        subtotal_minor=subtotal,
        service_fee_minor=service_fee,
        total_minor=total,
        estimated_ready_in_minutes=ESTIMATED_PREP_MINUTES,
    )


@router.post("/orders", response_model=OrderCreateResponse)
async def place_order(
    request: OrderPreviewRequest,
    idempotency_key: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header required")
    if current_user.role != "student":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only students may place orders")

    student_stmt = select(Student).where(Student.user_id == current_user.id)
    student_result = await session.execute(student_stmt)
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Student profile not found for current user")

    vendor_stmt = select(Vendor).where(Vendor.id == request.vendor_id)
    vendor_result = await session.execute(vendor_stmt)
    vendor = vendor_result.scalar_one_or_none()
    if not vendor or vendor.status != VendorStatus.approved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found or not approved")
    if not vendor.accepting_orders:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vendor is not accepting orders")

    current_minute = datetime.utcnow().hour * 60 + datetime.utcnow().minute
    if vendor.opens_at_minutes is not None and vendor.closes_at_minutes is not None:
        if vendor.opens_at_minutes <= vendor.closes_at_minutes:
            open_window = vendor.opens_at_minutes <= current_minute < vendor.closes_at_minutes
        else:
            open_window = current_minute >= vendor.opens_at_minutes or current_minute < vendor.closes_at_minutes
        if not open_window:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vendor is currently closed")

    if not request.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order must include at least one item")

    menu_item_ids = [item.menu_item_id for item in request.items]
    menu_stmt = select(MenuItem).where(MenuItem.id.in_(menu_item_ids))
    menu_result = await session.execute(menu_stmt)
    menu_items = menu_result.scalars().all()
    if len(menu_items) != len(menu_item_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more menu items are invalid")

    existing_order_stmt = select(Order).where(Order.idempotency_key == idempotency_key, Order.student_id == student.id)
    existing_order_result = await session.execute(existing_order_stmt)
    existing_order = existing_order_result.scalar_one_or_none()
    if existing_order:
        wallet_stmt = select(Wallet).where(Wallet.student_id == student.id)
        wallet_result = await session.execute(wallet_stmt)
        wallet = wallet_result.scalar_one_or_none()
        return OrderCreateResponse(
            id=existing_order.id,
            code=existing_order.code,
            status=existing_order.status,
            subtotal_minor=existing_order.subtotal_minor,
            service_fee_minor=existing_order.service_fee_minor,
            total_minor=existing_order.total_minor,
            wallet_balance_after_minor=wallet.balance_minor if wallet else 0,
            placed_at=existing_order.placed_at,
        )

    item_map = {str(item.id): item for item in menu_items}
    quantities = []
    for item_req in request.items:
        if item_req.menu_item_id not in item_map:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Menu item {item_req.menu_item_id} not found")
        if item_req.quantity <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Item quantities must be greater than zero")
        menu_item = item_map[item_req.menu_item_id]
        if menu_item.vendor_id != vendor.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Menu item {menu_item.name} does not belong to the selected vendor")
        if not menu_item.available:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Menu item {menu_item.name} is not available")
        if menu_item.stock_count < item_req.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Not enough stock for {menu_item.name}",
            )
        quantities.append(item_req.quantity)

    subtotal, service_fee, total = compute_order_totals(menu_items, quantities)

    wallet_stmt = select(Wallet).where(Wallet.student_id == student.id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    if wallet.frozen:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Wallet is frozen")
    if wallet.balance_minor < total:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Insufficient wallet balance")
    if wallet.blocked_categories:
        blocked = [item.name for item in menu_items if item.category in wallet.blocked_categories]
        if blocked:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Wallet blocks category: {blocked[0]}")

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today - timedelta(days=today.weekday())
    if wallet.daily_limit_minor is not None:
        spent_today = await _sum_purchase_spend(student.id, today, session)
        if spent_today + total > wallet.daily_limit_minor:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Daily spending limit exceeded")
    if wallet.weekly_limit_minor is not None:
        spent_week = await _sum_purchase_spend(student.id, week_start, session)
        if spent_week + total > wallet.weekly_limit_minor:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Weekly spending limit exceeded")

    order = Order(
        id=uuid4(),
        code=f"ORD-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{str(uuid4())[:8]}",
        student_id=student.id,
        school_id=student.school_id,
        vendor_id=vendor.id,
        subtotal_minor=subtotal,
        service_fee_minor=service_fee,
        total_minor=total,
        status=OrderStatus.pending,
        pickup_slot=request.pickup_slot,
        note=request.note,
        idempotency_key=idempotency_key,
        placed_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    # `session` already has a transaction auto-begun from the SELECTs above
    # (SQLAlchemy async sessions auto-begin on first use), so wrapping this in
    # `async with session.begin():` raised "A transaction is already begun on
    # this Session." on every single order placement. Just add everything and
    # commit once at the end — it's still one atomic transaction either way.
    session.add(order)
    await session.flush()

    for item_req in request.items:
        menu_item = item_map[item_req.menu_item_id]
        line = OrderLine(
            id=uuid4(),
            order_id=order.id,
            menu_item_id=menu_item.id,
            name=menu_item.name,
            art_key=menu_item.art_key,
            unit_price_minor=menu_item.price_minor,
            quantity=item_req.quantity,
        )
        session.add(line)
        menu_item.stock_count = max(menu_item.stock_count - item_req.quantity, 0)
        session.add(menu_item)

    wallet.balance_minor -= total
    wallet.updated_at = datetime.utcnow()
    session.add(wallet)
    transaction = WalletTransaction(
        id=uuid4(),
        wallet_id=wallet.id,
        student_id=student.id,
        type="purchase",
        amount_minor=-total,
        balance_after_minor=wallet.balance_minor,
        description=f"Purchase order {order.code}",
        reference=str(order.id),
        order_id=order.id,
        actor_id=current_user.id,
        created_at=datetime.utcnow(),
    )
    session.add(transaction)
    order_event = OrderEvent(
        id=uuid4(),
        order_id=order.id,
        status=OrderStatus.pending,
        actor_id=current_user.id,
        note="Order placed",
        created_at=datetime.utcnow(),
    )
    session.add(order_event)

    await session.commit()
    await session.refresh(order)

    return OrderCreateResponse(
        id=order.id,
        code=order.code,
        status=order.status,
        subtotal_minor=order.subtotal_minor,
        service_fee_minor=order.service_fee_minor,
        total_minor=order.total_minor,
        wallet_balance_after_minor=wallet.balance_minor,
        placed_at=order.placed_at,
    )


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
async def get_order(order_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    order_stmt = select(Order).where(Order.id == order_id)
    order_result = await session.execute(order_stmt)
    order = order_result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    await authorize_order_access(order, current_user, session)

    line_stmt = select(OrderLine).where(OrderLine.order_id == order.id)
    line_result = await session.execute(line_stmt)
    lines = line_result.scalars().all()

    return OrderDetailResponse(
        id=str(order.id),
        code=order.code,
        student_id=str(order.student_id),
        school_id=str(order.school_id),
        vendor_id=str(order.vendor_id),
        subtotal_minor=order.subtotal_minor,
        service_fee_minor=order.service_fee_minor,
        total_minor=order.total_minor,
        status=order.status,
        pickup_slot=order.pickup_slot,
        note=order.note,
        placed_at=order.placed_at,
        updated_at=order.updated_at,
        lines=lines,
    )


@router.get("/students/{student_id}/orders", response_model=List[OrderSummaryResponse])
async def get_student_orders(student_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if current_user.role == "student":
        student_stmt = select(Student).where(Student.user_id == current_user.id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or str(student.id) != student_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")
    elif current_user.role == "parent":
        parent_stmt = select(Parent).where(Parent.user_id == current_user.id)
        parent_result = await session.execute(parent_stmt)
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")
        guardianship_stmt = select(Guardianship).where(Guardianship.parent_id == parent.id, Guardianship.student_id == student_id)
        guardianship_result = await session.execute(guardianship_stmt)
        if not guardianship_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")
    elif current_user.role == "school_admin":
        student_stmt = select(Student).where(Student.id == student_id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.school_id != current_user.school_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")
    elif current_user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")

    orders_stmt = select(Order).where(Order.student_id == student_id).order_by(Order.placed_at.desc())
    orders_result = await session.execute(orders_stmt)
    return orders_result.scalars().all()


@router.get("/vendors/{vendor_id}/orders", response_model=List[OrderSummaryResponse])
async def get_vendor_orders(vendor_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if current_user.role == "vendor":
        vendor_stmt = select(Vendor).where(Vendor.user_id == current_user.id)
        vendor_result = await session.execute(vendor_stmt)
        vendor = vendor_result.scalar_one_or_none()
        if not vendor or str(vendor.id) != vendor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")
    elif current_user.role not in {"school_admin", "super_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access these orders")

    orders_stmt = select(Order).where(Order.vendor_id == vendor_id).order_by(Order.placed_at.desc())
    orders_result = await session.execute(orders_stmt)
    return orders_result.scalars().all()


@router.post("/orders/{order_id}/transition", response_model=OrderDetailResponse)
async def transition_order(
    order_id: str,
    request: OrderTransitionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if request.new_status not in ALLOWED_TRANSITIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target status")

    order_stmt = select(Order).where(Order.id == order_id)
    order_result = await session.execute(order_stmt)
    order = order_result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    TRANSITION_MATRIX = {
        OrderStatus.pending: {OrderStatus.accepted, OrderStatus.rejected, OrderStatus.cancelled},
        OrderStatus.accepted: {OrderStatus.preparing, OrderStatus.cancelled},
        OrderStatus.preparing: {OrderStatus.ready},
        OrderStatus.ready: {OrderStatus.completed},
    }

    if order.status not in TRANSITION_MATRIX:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order may not be transitioned from its current status")

    if current_user.role == "vendor":
        vendor_stmt = select(Vendor).where(Vendor.user_id == current_user.id)
        vendor_result = await session.execute(vendor_stmt)
        vendor = vendor_result.scalar_one_or_none()
        if not vendor or vendor.id != order.vendor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")
        if request.new_status not in TRANSITION_MATRIX[order.status]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invalid transition")
    elif current_user.role == "student":
        student_stmt = select(Student).where(Student.user_id == current_user.id)
        student_result = await session.execute(student_stmt)
        student = student_result.scalar_one_or_none()
        if not student or student.id != order.student_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this order")
        if request.new_status != OrderStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Students may only cancel their own orders")
        if request.new_status not in TRANSITION_MATRIX[order.status]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invalid transition")
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors or students may transition orders")

    if request.new_status in {OrderStatus.rejected, OrderStatus.cancelled}:
        wallet_stmt = select(Wallet).where(Wallet.student_id == order.student_id)
        wallet_result = await session.execute(wallet_stmt)
        wallet = wallet_result.scalar_one_or_none()
        if wallet:
            wallet.balance_minor += order.total_minor
            session.add(wallet)
            transaction = WalletTransaction(
                id=uuid4(),
                wallet_id=wallet.id,
                student_id=wallet.student_id,
                type="refund",
                amount_minor=order.total_minor,
                balance_after_minor=wallet.balance_minor,
                description=f"Refund for order {order.code}",
                reference=str(order.id),
                order_id=order.id,
                actor_id=current_user.id,
                created_at=datetime.utcnow(),
            )
            session.add(transaction)

    order.status = request.new_status
    order.updated_at = datetime.utcnow()
    session.add(order)

    order_event = OrderEvent(
        id=uuid4(),
        order_id=order.id,
        status=request.new_status,
        actor_id=current_user.id,
        note=request.note,
        created_at=datetime.utcnow(),
    )
    session.add(order_event)
    await session.commit()

    # `client.ts`'s orders.transition() maps this response the same way as
    # GET /orders/{id} (both feed the same `mapOrder`), which needs the full
    # order shape including line items — not just an acknowledgement. Without
    # this, the app's own mapper crashes reading `.lines` off `undefined`.
    line_stmt = select(OrderLine).where(OrderLine.order_id == order.id)
    line_result = await session.execute(line_stmt)
    lines = line_result.scalars().all()

    return OrderDetailResponse(
        id=order.id,
        code=order.code,
        student_id=order.student_id,
        school_id=order.school_id,
        vendor_id=order.vendor_id,
        subtotal_minor=order.subtotal_minor,
        service_fee_minor=order.service_fee_minor,
        total_minor=order.total_minor,
        status=order.status,
        pickup_slot=order.pickup_slot,
        note=order.note,
        placed_at=order.placed_at,
        updated_at=order.updated_at,
        lines=lines,
    )
