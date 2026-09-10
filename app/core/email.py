from typing import Optional

import httpx

from app.core.config import settings

# The API key goes in a header, not the JSON body — v4 authenticates via
# `X-ElasticEmail-ApiKey`, unlike the old v2 API's `apikey` query/body param.
# Getting this wrong doesn't fail loudly: the endpoint below still answers
# with a 400, and its body ("APIKey Expired") reads exactly like a dead key
# even when the real key was simply never presented the way v4 expects it.
_ELASTICEMAIL_URL = "https://api.elasticemail.com/v4/emails/transactional"


async def send_email(recipient: str, subject: str, html_body: str, text_body: Optional[str] = None) -> None:
    payload = {
        "Recipients": {"To": [recipient]},
        "Content": {
            "From": f"Y3ndidi <{settings.elasticemail_sender}>",
            "Subject": subject,
            "Body": [
                {"ContentType": "HTML", "Charset": "utf-8", "Content": html_body},
                {"ContentType": "PlainText", "Charset": "utf-8", "Content": text_body or html_body},
            ],
        },
    }
    headers = {"X-ElasticEmail-ApiKey": settings.elasticemail_api_key}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(_ELASTICEMAIL_URL, json=payload, headers=headers)
        response.raise_for_status()
