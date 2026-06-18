"""
Customers domain.

Replaces three different old concepts with three distinct models:

  - Customer: a tenant-scoped CRM record (name/email/phone/notes) FK'd to
    `core.Business`, exactly like every other domain table under the new
    tenancy model (see core/models.py). This is the direct replacement for
    the old `customers` table, except FK'd to Business instead of carrying
    a direct `user_id` to the business owner. A Customer never requires a
    login — most won't have one.

  - CustomerProfile: a OneToOne extension of `authentication.User` for the
    subset of customers who *do* have portal/login access. Under unified
    auth there's no separate portal account/session system any more (see
    README "Auth model"), so this just attaches customer-facing profile
    data (and email verification status, which the old
    customer_portal_accounts table never tracked) to the same User row
    everyone else authenticates as.

  - CustomerBusinessLink: join table between CustomerProfile and Business,
    replacing customer_business_relationships / customer_portal_links, so
    one logged-in customer can be linked to several businesses (e.g. a
    multi-location chain, or several unrelated businesses they patronize).

Note Customer and CustomerProfile are intentionally not FK'd to each other:
a walk-in Customer row and a portal CustomerProfile are different concerns
that happen to often describe the same person. Linking them is left for
when the domain that needs it (e.g. loyalty) is built, rather than guessing
the right join now.
"""

import uuid

from django.conf import settings as django_settings
from django.db import models

from core.models import Business


class Customer(models.Model):
    """A business's CRM record for a customer. No login required to exist."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="customers")

    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # Mirrors the old table's de-dupe behavior: a business
            # shouldn't end up with two records for the same email.
            # Multiple customers with no email (blank) are still allowed.
            models.UniqueConstraint(
                fields=["business", "email"],
                condition=~models.Q(email=""),
                name="unique_customer_email_per_business",
            ),
        ]

    def __str__(self):
        return self.name


class CustomerProfile(models.Model):
    """
    Portal/login identity for a customer. OneToOne with the same `User`
    table everyone else authenticates as (see authentication/models.py) —
    there is no separate password or session scheme for customers.
    """

    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
    )
    full_name = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")

    # The old customer_portal_accounts table never tracked this at all —
    # added now per the Phase 1 audit finding.
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    businesses = models.ManyToManyField(
        Business,
        through="CustomerBusinessLink",
        related_name="customer_profiles",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.full_name or self.user.email


class CustomerBusinessLink(models.Model):
    """
    Join table letting one CustomerProfile belong to several Businesses.
    Replaces customer_business_relationships / customer_portal_links.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer_profile = models.ForeignKey(
        CustomerProfile, on_delete=models.CASCADE, related_name="business_links"
    )
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="customer_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer_profile", "business"], name="unique_customer_profile_per_business"
            ),
        ]
        ordering = ["business"]

    def __str__(self):
        return f"{self.customer_profile} @ {self.business}"
