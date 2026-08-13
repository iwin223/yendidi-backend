from typing import Any, Dict

import httpx

from app.core.config import settings

PAYSTACK_INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"


class PaystackError(Exception):
    """Raised when Paystack rejects or fails a transaction request. Carries the
    processor's own message so the caller can surface a clean 4xx instead of
    letting an unhandled httpx exception fall through to a bare 500."""


async def create_paystack_transaction(amount_minor: int, email: str, payer_reference: str, topup_id: str) -> Dict[str, Any]:
    # Paystack requires `email` to be a real email address for its own receipts/
    # customer record — `payer_reference` is the momo number or masked card the
    # app shows on its own receipt, which is a different thing and not always
    # (usually isn't) a valid email, so it can't be reused for this field.
    #
    # Paystack's `amount` field is already in the currency's smallest unit
    # (pesewas for GHS) — the same unit our own amount_minor is in. Multiplying
    # by 100 here charged the payer 100x the amount they approved.
    data = {
        "amount": amount_minor,
        "email": email,
        "reference": topup_id,
        "metadata": {
            "topup_id": topup_id,
            "payer_reference": payer_reference,
        },
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                PAYSTACK_INITIALIZE_URL,
                json=data,
                headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                message = exc.response.json().get("message", "Paystack rejected the request.")
            except ValueError:
                message = "Paystack rejected the request."
            raise PaystackError(message) from exc
        except httpx.HTTPError as exc:
            raise PaystackError("Could not reach Paystack. Please try again.") from exc
        payload = response.json()
        if not payload.get("status"):
            raise PaystackError(payload.get("message", "Paystack initialization failed"))
        return payload.get("data", {})
