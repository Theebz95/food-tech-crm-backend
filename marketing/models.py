"""
Marketing / website tracking domain.

  tracking_scripts (old, implicit)  -> TrackingScript
  website_visitors / analytics_sessions (old) -> WebsiteVisitor
  page_views          (old) -> PageView
  tracking_events     (old) -> TrackingEvent
  leads               (old) -> Lead
  form_submissions    (old) -> FormSubmission
  google_ads_campaigns (old) -> GoogleAdsCampaign

The defining constraint of this domain (Phase 1 audit finding): the
tracking beacon and form-submission endpoints are public and
unauthenticated *by necessity* — they're called from arbitrary visitor
browsers on a business's own website, identified only by `script_key`,
which is embedded in client-side JS source. `script_key` therefore
identifies *which business*, never *who's authorized* — it can never
function as a real secret, no matter how it's generated. See
`marketing/public_views.py` and README "Marketing domain" for what
actually defends these endpoints (server-side rate limiting, payload
validation, and never trusting client-supplied visitor identity), since it
isn't, and can't be, the key itself.

The old rate limiter (`useRateLimiter.ts`) was client-side, localStorage-based
— meaningless against a real abuser, who simply doesn't run that JS and
hits the endpoint directly. That limiter doesn't get ported; it's replaced
entirely by server-side throttling (`marketing/throttles.py`).
"""

import uuid

from django.core.validators import RegexValidator
from django.db import models

from core.models import Business

from .encryption import EncryptedTextField

phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)


class TrackingScript(models.Model):
    """
    One business can have more than one (e.g. separate scripts for a
    marketing site vs. an app subdomain) — there's no reason to force a
    single key. `script_key` is generated server-side
    (`marketing.services.generate_script_key`, `secrets.token_urlsafe`) —
    never client-chosen, never sequential. Revoking a compromised key is
    just `is_active=False`; rotating it is the `regenerate-key` action
    (`views.py`), which replaces the value in place rather than requiring
    a new row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="tracking_scripts")
    script_key = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["business", "-created_at"]

    def __str__(self):
        return f"Tracking script for {self.business} ({'active' if self.is_active else 'revoked'})"


class WebsiteVisitor(models.Model):
    """
    Anonymous, server-assigned identity. This row's own `id` is the
    identifier set in the visitor's cookie (`marketing.services.VISITOR_COOKIE_NAME`)
    — never a value the client supplies in a request body. See
    `services.get_or_create_visitor` for why a cookie value that doesn't
    resolve to a row for *this* business is always treated as "no
    visitor yet," not adopted as-is.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="website_visitors")
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    # Abuse heuristic flag — see services._flag_if_high_frequency. Flags,
    # never blocks; a foundation for the business to see "this traffic
    # looks fake," not a full bot-detection system.
    is_suspicious = models.BooleanField(default=False)
    flagged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return f"Visitor {self.id} @ {self.business}"


class PageView(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visitor = models.ForeignKey(WebsiteVisitor, on_delete=models.CASCADE, related_name="page_views")
    url = models.CharField(max_length=2048)
    referrer = models.CharField(max_length=2048, blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    @property
    def business(self):
        return self.visitor.business

    def __str__(self):
        return f"{self.visitor_id} viewed {self.url}"


class TrackingEvent(models.Model):
    """
    `event_type` is validated server-side against this fixed set
    (`TrackEventSerializer`) — never an arbitrary, unbounded client
    string. `metadata` is size-capped at the serializer level
    (`marketing.serializers.MAX_METADATA_BYTES`) for the same reason
    `FloorPlan.layout` is validated in the Reservations domain: an
    unvalidated JSONField on a public, unauthenticated endpoint is an
    open invitation to store arbitrarily large payloads.
    """

    class EventType(models.TextChoices):
        CLICK = "click", "Click"
        SCROLL = "scroll", "Scroll"
        FORM_VIEW = "form_view", "Form view"
        OUTBOUND_LINK = "outbound_link", "Outbound link"
        CONVERSION = "conversion", "Conversion"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visitor = models.ForeignKey(WebsiteVisitor, on_delete=models.CASCADE, related_name="tracking_events")
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    metadata = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    @property
    def business(self):
        return self.visitor.business

    def __str__(self):
        return f"{self.visitor_id} {self.event_type}"


class GoogleAdsCampaign(models.Model):
    """
    `access_token`/`refresh_token` use `EncryptedTextField`
    (`marketing/encryption.py`) — Fernet-encrypted at rest, never a plain
    `CharField` — and are write-only on the serializer (never returned by
    the API, decrypted or otherwise; see `GoogleAdsCampaignSerializer`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="google_ads_campaigns")
    name = models.CharField(max_length=255)
    external_campaign_id = models.CharField(
        max_length=128, blank=True, default="", help_text="Google Ads' own campaign ID, once synced."
    )
    status = models.CharField(max_length=32, blank=True, default="", help_text="Google's reported campaign status.")
    is_active = models.BooleanField(default=True, help_text="Whether we're actively managing/syncing this campaign.")
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_campaign_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return f"{self.name} @ {self.business}"


class Lead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="leads")
    name = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="", validators=[phone_validator])
    # Basic attribution — "where did this lead come from."
    source = models.CharField(
        max_length=64, blank=True, default="", help_text='e.g. "website_form", "google_ads", "manual".'
    )
    utm_source = models.CharField(max_length=128, blank=True, default="")
    utm_medium = models.CharField(max_length=128, blank=True, default="")
    utm_campaign = models.CharField(max_length=128, blank=True, default="")
    google_ads_campaign = models.ForeignKey(
        GoogleAdsCampaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads"
    )
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Mirrors Customer's per-business email de-dupe — also what
            # makes services.submit_form's get_or_create-by-email safe.
            models.UniqueConstraint(
                fields=["business", "email"], condition=~models.Q(email=""), name="unique_lead_email_per_business"
            ),
        ]

    def __str__(self):
        return self.name or self.email or str(self.id)


class FormSubmission(models.Model):
    """
    `ip_address` is stored for abuse investigation only — deliberately
    never included in `FormSubmissionSerializer`'s fields, so it can't be
    returned by the API at all (not even read-only). Visible only via
    Django admin / direct DB access for genuine investigation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="form_submissions")
    lead = models.ForeignKey(
        Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name="form_submissions"
    )
    form_data = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"Form submission @ {self.business} ({self.submitted_at:%Y-%m-%d %H:%M})"
