from typing import Optional

import httpx

from app.core.config import settings


async def send_email(recipient: str, subject: str, html_body: str, text_body: Optional[str] = None) -> None:
    payload = {
        "apikey": settings.elasticemail_api_key,
        "from": settings.elasticemail_sender,
        "fromName": "Y3ndidi",
        "to": recipient,
        "subject": subject,
        "bodyHtml": html_body,
        "bodyText": text_body or html_body,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://api.elasticemail.com/v4/emails", json=payload)
        response.raise_for_status()
