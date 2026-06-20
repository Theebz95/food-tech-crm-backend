"""
Business settings domain.

Most of the old settings-shaped tables already have a home elsewhere —
see README "Settings domain" for the full old-table -> new-home mapping
so there's no confusion later about where to look for a given setting:

  geofence_settings         -> employees.GeofenceSetting
  business_hours            -> reservations.BusinessHours
  customer_portal_settings  -> moot under unified auth (see customers/models.py)
  reservation_settings      -> reservations.ReservationSetting

What's actually left for this app is business-level profile/preferences —
logo, contact info, address, a business-wide default timezone, and
notification preference toggles — which is what `BusinessProfile` is.

`BusinessProfile.logo` is a real `ForeignKey` to `documents.Document`, not
a second copy of Document's storage_key/status fields. Uploading,
replacing, or removing a logo goes through `documents.services.upload_document`/
`delete_document` directly (see settings/services.py) — reusing the
Documents domain's write-row-first orphan-prevention flow rather than
building a second, parallel one. This is a deliberate exception to the
"every domain only depends on core" convention followed elsewhere in this
codebase; justified here because duplicating that flow's pending/uploaded/failed
state machine for one more file field would be exactly the kind of
copy-paste this domain is explicitly avoiding.
"""

import uuid

from django.core.validators import RegexValidator
from django.db import models

from core.models import Business
from documents.models import Document

phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)


class BusinessProfile(models.Model):
    """One row per business — same singleton-per-business convention as reservations.ReservationSetting."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="profile")
    logo = models.ForeignKey(
        Document,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Set via the upload-logo action (settings/services.py:set_logo), never written directly.",
    )

    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=32, blank=True, default="", validators=[phone_validator])
    address = models.CharField(max_length=512, blank=True, default="")
    # Business-wide fallback, distinct from any one BusinessLocation's own
    # `timezone` field (core.models.BusinessLocation) — e.g. for reporting
    # at the business level, or as the default when a new location is
    # created without specifying one.
    default_timezone = models.CharField(max_length=64, default="UTC")

    # Notification preferences — storage/toggle only; no notification
    # delivery (email sending) is wired up to read these yet. Limited to
    # events from domains that actually exist today rather than
    # speculating about ones that don't.
    email_on_new_reservation = models.BooleanField(default=True)
    email_on_low_stock = models.BooleanField(default=True)
    email_on_new_lead = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile for {self.business}"
