from uuid import uuid4

from app.api.orders import compute_order_totals
from app.db.models import FoodCategory, MenuItem


def test_compute_order_totals_applies_service_fee():
    items = [
        MenuItem(
            id=uuid4(),
            vendor_id=uuid4(),
            name="Plain rice",
            category=FoodCategory.lunch,
            price_minor=3500,
            art_key="plain_rice",
        ),
        MenuItem(
            id=uuid4(),
            vendor_id=uuid4(),
            name="Mango juice",
            category=FoodCategory.drinks,
            price_minor=1500,
            art_key="mango_juice",
        ),
    ]
    quantities = [1, 2]

    subtotal, service_fee, total = compute_order_totals(items, quantities)

    assert subtotal == 6500
    assert service_fee == 325
    assert total == 6825
