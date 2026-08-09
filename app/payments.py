from typing import Any, Dict

import httpx

from app.core.config import settings

PAYSTACK_INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"


async def create_paystack_transaction(amount_minor: int, payer_reference: str, topup_id: str) -> Dict[str, Any]:
    amount_kobo = amount_minor * 100
    data = {
        "amount": amount_kobo,
        "email": payer_reference,
        "reference": topup_id,
        "metadata": {
            "topup_id": topup_id,
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            PAYSTACK_INITIALIZE_URL,
            json=data,
            headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("status"):
            raise RuntimeError("Paystack initialization failed")
        return payload.get("data", {})
