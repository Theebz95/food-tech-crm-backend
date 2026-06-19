"""
Reservations domain: restaurant tables, floor plans, business hours,
blackout dates, reservation settings, reservations, and a waitlist.

  restaurant_tables    (old) -> RestaurantTable
  floor_plans          (old) -> FloorPlan
  business_hours       (old) -> BusinessHours
  blackout_dates       (old) -> BlackoutDate
  reservation_settings (old) -> ReservationSetting
  reservations         (old) -> Reservation
  waitlist             (old) -> Waitlist

Every model here FKs to `core.BusinessLocation` (required, not an optional
business-wide fallback like `GeofenceSetting` elsewhere in this codebase) —
except `ReservationSetting`, which is genuinely business-wide. A
reservation is always "a table, at one location, at one time"; there's no
sensible business-wide table or floor plan. A single-location restaurant
simply creates exactly one `BusinessLocation` row to use this domain — see
README "Reservations domain" for the rationale.

Security/data-integrity fixes versus the old system (Phase 1 audit):

  1. No concurrency control existed on booking — two guests could book the
     same table/slot simultaneously. `reservations/services.py:book_reservation`
     wraps table assignment in `transaction.atomic()` + `select_for_update()`
     on the candidate `RestaurantTable` rows, closing that race.

  2. `business_hours` used to be publicly readable in a way that let anyone
     enumerate every business's hours by ID. The guest-facing read endpoint
     (`reservations/public_views.py`) only ever resolves hours for one
     business + location named explicitly in the URL; there is no route
     that lists hours across businesses or locations.

  3. The floor plan JSONB had no schema validation and wasn't synced to
     `RestaurantTable`'s position columns, so the two could drift.
     `FloorPlan.layout` therefore never stores x/y itself — only table
     references and non-positional metadata (rotation/label) —
     `FloorPlanSerializer` validates every referenced table id is a real
     `RestaurantTable` for that location. `position_x`/`position_y` on
     `RestaurantTable` are the only source of truth for where a table is.

  4. confirmation_code generation and end_time calculation were Postgres
     triggers (`generate_confirmation_code()`/`set_confirmation_code`,
     `calculate_reservation_end_time()`/`set_reservation_end_time` — see
     the Phase 1 SQL audit). Both are now `Reservation.save()` logic. The
     original trigger never handled a confirmation_code collision (the
     column's unique constraint just raised); `save()` now retries
     generation on collision instead of crashing.
"""

import secrets
import uuid
from datetime import timedelta

from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Q

from core.models import Business, BusinessLocation

DEFAULT_DURATION_MINUTES = 90
CONFIRMATION_CODE_MAX_ATTEMPTS = 8


def _generate_confirmation_code():
    """
    6-char uppercase hex, matching the shape of the old
    md5(random())-based trigger's output. A module-level function (rather
    than inlined in save()) specifically so tests can monkeypatch it to
    force a collision and exercise the retry path.
    """
    return secrets.token_hex(3).upper()


class RestaurantTable(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="tables")
    name = models.CharField(max_length=64)
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    # Source of truth for where this table sits on the floor plan canvas —
    # see module docstring fix #3. FloorPlan.layout never stores these.
    position_x = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    position_y = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["location", "name"], name="unique_table_name_per_location"),
        ]
        ordering = ["location", "name"]

    @property
    def business(self):
        return self.location.business

    def __str__(self):
        return f"{self.name} @ {self.location}"


class FloorPlan(models.Model):
    """
    `layout` only ever references RestaurantTable ids + non-positional
    metadata (e.g. rotation, a display shape/label) — never x/y, so it
    can't drift from RestaurantTable's position columns. Named (not a
    business-wide singleton) so a location can have more than one, e.g.
    "Indoor" / "Patio".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="floor_plans")
    name = models.CharField(max_length=255, default="Main", blank=True)
    layout = models.JSONField(
        default=dict,
        blank=True,
        help_text='e.g. {"tables": [{"table_id": "<uuid>", "rotation": 0}], "elements": [...]}.',
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["location", "name"]

    @property
    def business(self):
        return self.location.business

    def __str__(self):
        return f"{self.name} @ {self.location}"


class BusinessHours(models.Model):
    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="business_hours")
    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    open_time = models.TimeField(null=True, blank=True)
    close_time = models.TimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["location", "day_of_week"], name="unique_hours_per_location_day"),
        ]
        ordering = ["location", "day_of_week"]

    @property
    def business(self):
        return self.location.business

    def __str__(self):
        if self.is_closed:
            return f"{self.location} {self.get_day_of_week_display()}: closed"
        return f"{self.location} {self.get_day_of_week_display()}: {self.open_time}-{self.close_time}"


class BlackoutDate(models.Model):
    """A date reservations can't be made for at a location (holiday, private event, etc)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="blackout_dates")
    date = models.DateField()
    reason = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["location", "date"], name="unique_blackout_per_location_date"),
        ]
        ordering = ["date"]

    @property
    def business(self):
        return self.location.business

    def __str__(self):
        return f"{self.location} blacked out {self.date}"


class ReservationSetting(models.Model):
    """
    One row per Business (not per location — booking window/buffer/party
    size policy is set at the business level, same granularity as the old
    `reservation_settings` table).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="reservation_setting")
    default_duration_minutes = models.PositiveIntegerField(
        default=DEFAULT_DURATION_MINUTES, validators=[MinValueValidator(1)]
    )
    slot_interval_minutes = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)])
    buffer_minutes = models.PositiveIntegerField(
        default=0, help_text="Minimum gap enforced between two reservations on the same table."
    )
    min_advance_minutes = models.PositiveIntegerField(
        default=0, help_text="How last-minute a guest booking may be made."
    )
    max_advance_days = models.PositiveIntegerField(
        default=60, help_text="How far in advance a guest may book."
    )
    max_party_size = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1)])

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Reservation settings for {self.business}"


class Reservation(models.Model):
    """
    No User FK — guests booking through the public flow aren't
    authenticated at all (see reservations/public_views.py and README
    "Reservations domain" for why this permission model is intentionally
    separate from core.permissions.HasBusinessRole).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        SEATED = "seated", "Seated"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    # Statuses that still hold a table — used by services.py to compute
    # overlap when looking for an available table.
    ACTIVE_STATUSES = (Status.PENDING, Status.CONFIRMED, Status.SEATED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="reservations")
    table = models.ForeignKey(
        RestaurantTable,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
        help_text="Null until a table is assigned (by the booking service, or manually by staff).",
    )

    guest_name = models.CharField(max_length=255)
    guest_email = models.EmailField(blank=True, default="")
    guest_phone = models.CharField(max_length=32, blank=True, default="")
    party_size = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    start_time = models.DateTimeField()
    # Not nullable — save() always computes this before the row is
    # persisted (see below) — but never required as API input.
    end_time = models.DateTimeField()
    duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Resolved and stored by save() if not given: ReservationSetting.default_duration_minutes, or 90.",
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    confirmation_code = models.CharField(max_length=6, unique=True, editable=False)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_time"]
        constraints = [
            models.CheckConstraint(check=Q(end_time__gt=models.F("start_time")), name="reservation_end_after_start"),
        ]

    @property
    def business(self):
        return self.location.business

    def _resolve_duration_minutes(self):
        setting = ReservationSetting.objects.filter(business_id=self.location.business_id).first()
        return setting.default_duration_minutes if setting else DEFAULT_DURATION_MINUTES

    def save(self, *args, **kwargs):
        if self.duration_minutes is None:
            self.duration_minutes = self._resolve_duration_minutes()
        if not self.end_time:
            self.end_time = self.start_time + timedelta(minutes=self.duration_minutes)

        if self.confirmation_code:
            super().save(*args, **kwargs)
            return
        self._save_with_generated_confirmation_code(*args, **kwargs)

    def _save_with_generated_confirmation_code(self, *args, **kwargs):
        """
        The old trigger generated this once and let the unique constraint
        raise on collision. With a 6-char keyspace that's a real risk at
        scale, so this retries with a fresh code instead of failing the
        request — each attempt in its own savepoint so a failed INSERT
        doesn't poison an outer transaction.
        """
        last_error = None
        for _ in range(CONFIRMATION_CODE_MAX_ATTEMPTS):
            self.confirmation_code = _generate_confirmation_code()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                last_error = exc
                continue
        raise last_error

    def __str__(self):
        return f"{self.guest_name} {self.start_time:%Y-%m-%d %H:%M} ({self.confirmation_code})"


class Waitlist(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        NOTIFIED = "notified", "Notified"
        SEATED = "seated", "Seated"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(BusinessLocation, on_delete=models.CASCADE, related_name="waitlist_entries")
    guest_name = models.CharField(max_length=255)
    guest_email = models.EmailField(blank=True, default="")
    guest_phone = models.CharField(max_length=32, blank=True, default="")
    party_size = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    requested_time = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    reservation = models.OneToOneField(
        Reservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waitlist_entry",
        help_text="Set when staff convert this waitlist entry into a real Reservation.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["requested_time"]

    @property
    def business(self):
        return self.location.business

    def __str__(self):
        return f"{self.guest_name} waiting @ {self.location} ({self.status})"
