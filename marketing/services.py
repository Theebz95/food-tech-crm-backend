"""
Marketing service layer. The public tracking/form endpoints
(public_views.py) are deliberately thin — script_key resolution, visitor
identification, payload recording, and the abuse heuristic all live here
so they're testable independent of the HTTP layer.
"""

import secrets
import uuid
from datetime import timedelta
from typing import Optional

from django.utils import timezone

from .models import FormSubmission, Lead, PageView, TrackingEvent, TrackingScript, WebsiteVisitor

VISITOR_COOKIE_NAME = "ftc_vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

# Abuse heuristic — flags, never blocks (see models.py WebsiteVisitor.is_suspicious).
# 20 page views/events from one visitor inside 10 seconds is well beyond
# any plausible human browsing rate; it's a starting point for "this
# traffic looks automated," not a tuned production threshold.
HIGH_FREQUENCY_WINDOW = timedelta(seconds=10)
HIGH_FREQUENCY_THRESHOLD = 20


def generate_script_key() -> str:
    """Real random token (not sequential/guessable) — see TrackingScript docstring."""
    return secrets.token_urlsafe(32)


def resolve_script_key(script_key: str) -> Optional[TrackingScript]:
    """
    Returns the active TrackingScript for this key, or None. Every caller
    must treat every None identically — script_key is visible in
    client-side JS, so a nonexistent, malformed, or merely-inactive key
    must be indistinguishable from the caller's perspective (see
    public_views.py).
    """
    if not script_key:
        return None
    return TrackingScript.objects.filter(script_key=script_key, is_active=True).select_related("business").first()


def get_or_create_visitor(business, cookie_visitor_id: Optional[str]) -> WebsiteVisitor:
    """
    The visitor id is never accepted from the request body — only from a
    cookie this server already set. A cookie value that doesn't resolve
    to a WebsiteVisitor for *this* business (wrong business, garbage,
    tampered, or simply absent) always results in a brand-new visitor row
    rather than adopting whatever the client sent.
    """
    if cookie_visitor_id:
        try:
            visitor_uuid = uuid.UUID(cookie_visitor_id)
        except (ValueError, AttributeError, TypeError):
            visitor_uuid = None
        if visitor_uuid is not None:
            visitor = WebsiteVisitor.objects.filter(id=visitor_uuid, business=business).first()
            if visitor is not None:
                visitor.save(update_fields=["last_seen"])  # auto_now bumps last_seen
                return visitor
    return WebsiteVisitor.objects.create(business=business)


def _flag_if_high_frequency(visitor: WebsiteVisitor) -> None:
    if visitor.is_suspicious:
        return
    window_start = timezone.now() - HIGH_FREQUENCY_WINDOW
    recent_count = PageView.objects.filter(
        visitor=visitor, timestamp__gte=window_start
    ).count() + TrackingEvent.objects.filter(visitor=visitor, timestamp__gte=window_start).count()
    if recent_count >= HIGH_FREQUENCY_THRESHOLD:
        visitor.is_suspicious = True
        visitor.flagged_at = timezone.now()
        visitor.save(update_fields=["is_suspicious", "flagged_at"])


def record_pageview(visitor: WebsiteVisitor, url: str, referrer: str = "") -> PageView:
    page_view = PageView.objects.create(visitor=visitor, url=url, referrer=referrer)
    _flag_if_high_frequency(visitor)
    return page_view


def record_event(visitor: WebsiteVisitor, event_type: str, metadata: dict) -> TrackingEvent:
    event = TrackingEvent.objects.create(visitor=visitor, event_type=event_type, metadata=metadata)
    _flag_if_high_frequency(visitor)
    return event


def submit_form(business, form_data: dict, ip_address: Optional[str]) -> FormSubmission:
    """
    A form submission with an email gets linked to a Lead, de-duplicated
    per business by email (Lead.unique_lead_email_per_business) — repeat
    submissions from the same person update/reuse one Lead rather than
    piling up duplicates.
    """
    lead = None
    email = (form_data.get("email") or "").strip()
    if email:
        lead, _created = Lead.objects.get_or_create(
            business=business,
            email=email,
            defaults={
                "name": form_data.get("name", ""),
                "phone": form_data.get("phone", ""),
                "source": "website_form",
            },
        )
    return FormSubmission.objects.create(business=business, lead=lead, form_data=form_data, ip_address=ip_address)
