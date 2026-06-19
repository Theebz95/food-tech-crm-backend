"""
Employees / Time Tracking / Geofencing domain.

Scope of this pass: time tracking + server-side geofence verification only.
Scheduling/shifts and pay stubs are deferred to a follow-up session (see
README "Project status").

There is no separate Employee-as-user model. An "employee" is simply a
`core.BusinessMembership` with role `staff` or `manager` — every model here
FKs to BusinessMembership, which already identifies both the person
(`.user`, an `authentication.User`) and which Business (and optionally
which BusinessLocation) they belong to.

Core security fix (Phase 1 audit finding): the old frontend computed
Haversine distance in the browser (src/lib/geolocation.ts) and trusted the
client's own "within range" boolean — trivially spoofable by anyone who can
edit a JS variable or replay a request. Every distance/within-geofence value
stored here is computed server-side in `employees/services.py`, never
accepted from the client. Raw client-reported coordinates are stored too,
but only as the *input* to that computation, never as the verdict itself.

  geofence_settings (old)            -> GeofenceSetting
  time_tracking (old)                -> TimeEntry
  time_tracking_breaks (old)         -> TimeEntryBreak
  (new, no old equivalent)           -> LocationVerificationLog
"""

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import Business, BusinessLocation, BusinessMembership


class GeofenceSetting(models.Model):
    """
    Configures the allowed clock-in/out radius for a Business, optionally
    scoped to one BusinessLocation. A null `location` means "applies
    business-wide" — same convention as BusinessLocation.hours and every
    other business-wide-vs-location-scoped field in this codebase.

    `enabled=False` (or no row at all for the relevant business/location)
    means geofencing is not enforced — see services.get_applicable_geofence
    for the lookup order and services.verify_geofence for what happens when
    nothing applies.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="geofence_settings"
    )
    location = models.ForeignKey(
        BusinessLocation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="geofence_settings",
        help_text="Optional. Null applies business-wide.",
    )
    center_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, validators=[MinValueValidator(-90), MaxValueValidator(90)]
    )
    center_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, validators=[MinValueValidator(-180), MaxValueValidator(180)]
    )
    radius_meters = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "location"], name="unique_geofence_per_business_location"
            ),
            models.UniqueConstraint(
                fields=["business"],
                condition=Q(location__isnull=True),
                name="unique_geofence_per_business_when_business_wide",
            ),
        ]
        ordering = ["business", "location"]

    def __str__(self):
        scope = self.location.name if self.location_id else "business-wide"
        return f"{self.business.name} – {scope} ({self.radius_meters}m)"


class TimeEntry(models.Model):
    """
    One clock-in/clock-out session for a BusinessMembership. Replaces the
    old `time_tracking` table, which FK'd directly to a user_id; this FKs to
    BusinessMembership instead so it carries both "who" and "at which
    business/location" the way every domain table does under the new
    tenancy model.

    `clock_in_distance_meters` / `clock_in_within_geofence` (and the
    clock-out equivalents) are always computed by
    employees.services.verify_geofence — never accepted from the client.
    `clock_in_lat` / `clock_in_lng` are the raw client-reported coordinates,
    kept only as an audit trail of what was submitted.
    """

    class Status(models.TextChoices):
        CLOCKED_IN = "clocked_in", "Clocked in"
        CLOCKED_OUT = "clocked_out", "Clocked out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="time_entries"
    )

    clock_in_at = models.DateTimeField()
    clock_out_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CLOCKED_IN)

    clock_in_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_in_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_out_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_out_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Server-computed, audit-only. Null means no geofence applied (none
    # configured, or disabled) at the time of that clock-in/out.
    clock_in_distance_meters = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    clock_out_distance_meters = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    clock_in_within_geofence = models.BooleanField(null=True, blank=True)
    clock_out_within_geofence = models.BooleanField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-clock_in_at"]
        constraints = [
            # Mirrors the state-machine rule enforced in services.py at the
            # DB level: a membership can have at most one open (no
            # clock_out_at) entry at a time.
            models.UniqueConstraint(
                fields=["membership"],
                condition=Q(clock_out_at__isnull=True),
                name="unique_open_time_entry_per_membership",
            ),
        ]

    def __str__(self):
        return f"{self.membership} {self.clock_in_at:%Y-%m-%d %H:%M}"


class TimeEntryBreak(models.Model):
    """Replaces the old `time_tracking_breaks` table."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    time_entry = models.ForeignKey(TimeEntry, on_delete=models.CASCADE, related_name="breaks")
    break_start_at = models.DateTimeField()
    break_end_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-break_start_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["time_entry"],
                condition=Q(break_end_at__isnull=True),
                name="unique_open_break_per_time_entry",
            ),
        ]

    def __str__(self):
        return f"Break on {self.time_entry}"


class LocationVerificationLog(models.Model):
    """
    Audit trail of every geofence check attempt, including rejected ones —
    a TimeEntry only exists for a *successful* clock-in, so a hard-blocked
    out-of-geofence attempt would otherwise leave no trace at all. No old
    table equivalent; this is new infrastructure to make the security fix
    auditable, not just enforced.
    """

    class CheckType(models.TextChoices):
        CLOCK_IN = "clock_in", "Clock in"
        CLOCK_OUT = "clock_out", "Clock out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="location_verification_logs"
    )
    # Null when the check was rejected before a TimeEntry could be created
    # (e.g. an out-of-geofence clock-in attempt).
    time_entry = models.ForeignKey(
        TimeEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verification_logs",
    )
    geofence_setting = models.ForeignKey(
        GeofenceSetting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verification_logs",
        help_text="Which setting was evaluated, if any. Null if no geofence applied.",
    )
    check_type = models.CharField(max_length=16, choices=CheckType.choices)
    reported_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    reported_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    distance_meters = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    within_geofence = models.BooleanField(null=True, blank=True)
    # Whether the check allowed the action to proceed — True if within the
    # geofence, or if no geofence applied at all (nothing to enforce).
    passed = models.BooleanField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.check_type} check for {self.membership} ({'pass' if self.passed else 'fail'})"
