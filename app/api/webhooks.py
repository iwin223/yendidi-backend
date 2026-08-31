import hashlib
import hmac
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlmodel import select

from app.core.config import settings
from app.core.dependencies import get_session
from app.db.models import Topup, TopupStatus, Wallet, WalletTransaction, WebhookEvent
from app.db.session import AsyncSession

router = APIRouter()


@router.post("/elastic-email")
async def elastic_email_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    payload = await request.json()
    event = WebhookEvent(
        id=uuid4(),
        provider="elastic-email",
        event_type=payload.get("event", "unknown"),
        payload=payload,
    )
    session.add(event)
    await session.commit()
    return {"status": "received", "event": event.event_type}


def verify_paystack_signature(payload: bytes, signature: str) -> bool:
    # Paystack has no separate webhook-signing secret — it signs with the same
    # integration secret key used for API calls (unlike e.g. Stripe). Verifying
    # against PAYSTACK_WEBHOOK_SECRET (a placeholder that's never matched
    # anything Paystack actually sends) meant every real webhook call was
    # rejected with 401 regardless of test/live mode.
    computed = hmac.new(settings.paystack_secret_key.encode(), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature or "")


@router.post("/paystack")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None), session: AsyncSession = Depends(get_session)):
    body = await request.body()
    if not x_paystack_signature or not verify_paystack_signature(body, x_paystack_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event")
    data = payload.get("data", {})
    reference = data.get("reference")
    status_value = data.get("status")

    event_record = WebhookEvent(
        id=uuid4(),
        provider="paystack",
        event_type=event or "unknown",
        payload=payload,
    )
    session.add(event_record)
    await session.commit()

    if not reference:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing webhook reference")

    statement = select(Topup).where(Topup.id == reference)
    result = await session.execute(statement)
    topup = result.scalar_one_or_none()
    if not topup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topup not found")

    if topup.status == TopupStatus.succeeded:
        return {"status": "already_processed"}

    topup.status = TopupStatus.succeeded
    topup.processor_ref = data.get("reference")
    topup.settled_at = datetime.utcnow()
    session.add(topup)

    wallet_stmt = select(Wallet).where(Wallet.id == topup.wallet_id)
    wallet_result = await session.execute(wallet_stmt)
    wallet = wallet_result.scalar_one_or_none()
    if wallet:
        wallet.balance_minor += topup.amount_minor
        session.add(wallet)
        txn = WalletTransaction(
            id=uuid4(),
            wallet_id=wallet.id,
            student_id=wallet.student_id,
            type="topup",
            amount_minor=topup.amount_minor,
            balance_after_minor=wallet.balance_minor,
            description="Wallet top-up via Paystack",
            reference=topup.processor_ref or reference,
            created_at=datetime.utcnow(),
        )
        session.add(txn)

    await session.commit()
    return {"status": "processed"}
