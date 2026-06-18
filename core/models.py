"""
Core tenancy models.

Replaces the old Supabase schema's pattern of putting a business owner's
auth.users.id directly on every domain table (reservations.user_id,
customers.user_id, invoices.user_id, ...). Domain tables now FK to Business
(and optionally BusinessLocation), and "does this row belong to me" becomes
"does this user have an active BusinessMembership on this Business with
sufficient role" — see core/permissions.py.
"""

import uuid

from django.conf import settings as django_settings
from django.db import models


class Business(models.Model):
    """The tenant entity. One row per restaurant/business account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_businesses",
    )
    name = models.CharField(max_length=255)

    # Free-form business configuration. Named `extra_settings` rather than
    # `settings` to avoid shadowing `django.conf.settings` in this module.
    extra_settings = models.JSONField(default=dict, blank=True)

    # --- Subscription / trial lifecycle ---------------------------------
    # These fields exist specifically to support two things built in this
    # scaffold: the Stripe webhook (finance/webhooks.py) and the daily
    # trial-expiration Celery task (core/tasks.py), which replicates the old
    # check_expired_trials() Postgres function + pg_cron job.
    is_active = models.BooleanField(default=True)
    is_legacy = models.BooleanField(
        default=False,
        help_text="Grandfathered accounts that never expire regardless of trial/subscription status.",
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    trial_expired = models.BooleanField(default=False)
    subscription_status = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Mirrors the Stripe subscription status string (trialing, active, past_due, canceled, ...).",
    )
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class BusinessLocation(models.Model):
    """
    Optional secondary locations under a Business. A single-location
    business simply never creates one of these; domain tables that want to
    be location-scoped treat a null location FK as "applies business-wide".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="locations")
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512, blank=True, default="")
    timezone = models.CharField(max_length=64, default="UTC")
    hours = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-day open/close times, replacing the old business_hours table's "
            "one-row-per-day-of-week pattern. Exact shape to be finalized when "
            "the Reservations domain is built."
        ),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.business.name} – {self.name}"


class BusinessMembership(models.Model):
    """
    Join table between User and Business. This is the access-control
    primitive that replaces direct user_id-column scoping: every tenant
    permission check (see core/permissions.py) is "find an active
    membership for (business, user) with role >= required".
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        STAFF = "staff", "Staff"

    # Ordering used by permission checks to decide if a role is "sufficient".
    ROLE_RANK = {
        Role.STAFF: 1,
        Role.MANAGER: 2,
        Role.OWNER: 3,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="business_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STAFF)
    location = models.ForeignKey(
        BusinessLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
        help_text="Optional. Scopes a staff member to a single location; null means business-wide.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "user"], name="unique_membership_per_business"),
        ]
        ordering = ["business", "role"]

    def __str__(self):
        return f"{self.user} @ {self.business} ({self.role})"
