"""
Minimal Resend integration — the first email-sending infrastructure in
this codebase. No prior pattern existed to reuse (checked: no
`resend`/`send_mail`/`EmailMessage`/SMTP usage anywhere in this repo
before this); built here because the Loyalty domain's gift-card
purchase/email flow (loyalty/services.py:send_gift_card_email) needs it,
and structured as shared `core` infrastructure rather than something
loyalty-specific, since Finance's invoice-email flow (not yet built) is
an obvious, expected future caller.

Calls Resend's REST API directly via `requests` rather than pulling in
the `resend` SDK as a dependency — one POST, one response, not worth a
whole client library (same reasoning as documents/storage.py talking to
S3 directly rather than through a heavier abstraction).
"""

import requests
from django.conf import settings

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSendError(Exception):
    pass


def send_email(to: str, subject: str, html_body: str, from_email: str = None) -> None:
    response = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        json={
            "from": from_email or settings.RESEND_FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html_body,
        },
        timeout=10,
    )
    if response.status_code >= 400:
        raise EmailSendError(f"Resend API returned {response.status_code}: {response.text}")
