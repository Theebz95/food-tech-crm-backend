"""
Employees domain: time tracking + geofencing, scheduling, and pay stubs.

There is no separate Employee-as-user model anywhere in this app. An
"employee" is simply a `core.BusinessMembership` with role `staff` or
`manager` — every model here FKs to BusinessMembership (directly, or
transitively via EmployeeShift/TimeEntry), which already identifies both
the person (`.user`, an `authentication.User`) and which Business (and
optionally which BusinessLocation) they belong to.

Security fix #1 (Phase 1 audit finding, time tracking): the old frontend
computed Haversine distance in the browser (src/lib/geolocation.ts) and
trusted the client's own "within range" boolean — trivially spoofable.
Every distance/within-geofence value stored here is computed server-side in
`employees/services.py`, never accepted from the client.

Security fix #2 (Phase 1 audit finding, scheduling): recurring schedules
used to be expanded into visible shifts on-the-fly, client-side, on every
render — there was no durable row for "this employee's shift next
Tuesday" until someone's browser happened to compute it. `RecurringSchedule`
rows are now expanded into real, persisted `EmployeeShift` rows by a
Celery Beat task (`employees/tasks.py` + `employees/scheduling.py`).

Security fix #3 (Phase 1 audit finding, pay stubs): gross/net pay used to
be computed client-side with no overtime or tax logic at all.
`employees/payroll.py` computes it server-side from actual `TimeEntry`
hours. The tax portion is an explicit, clearly-marked placeholder — see
that module's docstring and the README disclaimer before this is anywhere
near real payroll.

  geofence_settings (old)            -> GeofenceSetting
  time_tracking (old)                -> TimeEntry
  time_tracking_breaks (old)         -> TimeEntryBreak
  (new, no old equivalent)           -> LocationVerificationLog
  positions (old)                    -> Position
  (new, no old equivalent)           -> EmployeeAvailability
  shift_templates (old)              -> ShiftTemplate
  recurring_schedules (old)          -> RecurringSchedule
  employee_shifts (old)              -> EmployeeShift
  shift_swap_requests (old)          -> ShiftSwapRequest
  time_off_requests (old)            -> TimeOffRequest
  pay_stubs (old)                    -> PayStub
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

    @property
    def business(self):
        # No direct `business` FK (only `membership`) — this lets
        # core.permissions.HasBusinessRole.has_object_permission resolve
        # the tenant via its `hasattr(obj, "business")` fallback without
        # any employees-specific changes to that shared permission class.
        return self.membership.business

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


class Position(models.Model):
    """
    A job role at a Business (e.g. "Server", "Line Cook"), carrying the
    hourly rate used for pay stub calculation.

    Design choice: the rate lives here, on Position, rather than on
    BusinessMembership or as a per-assignment override. Reasons:
      - BusinessMembership is a `core` model shared by every domain app;
        giving it an employees-specific "current position"/rate field
        would invert the dependency direction every other domain app
        follows (domains FK into core, core doesn't know about domains).
      - A membership can plausibly work more than one Position over time
        (or even concurrently, e.g. a manager who also covers shifts as
        a server) — there's no single "current rate" to store on it.
      - Pay stub generation (employees/payroll.py) therefore takes an
        explicit Position argument rather than inferring one, and PayStub
        records which Position's rate was used, for auditability.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="positions")
    name = models.CharField(max_length=255)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_position_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return f"{self.name} @ {self.business.name}"


class EmployeeAvailability(models.Model):
    """
    General weekly availability for a BusinessMembership — "I'm usually
    free Mondays 9am-5pm" — not a specific date. Day-of-week + time-range
    chosen over a date-range because the recurring-schedule use case this
    feeds (manually, for now — there's no automatic schedule-vs-availability
    matching yet) is inherently weekly.
    """

    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="availabilities"
    )
    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["membership", "day_of_week", "start_time"]

    @property
    def business(self):
        return self.membership.business

    def __str__(self):
        return f"{self.membership} available {self.get_day_of_week_display()} {self.start_time}-{self.end_time}"


class ShiftTemplate(models.Model):
    """
    A reusable shift definition — "Morning server shift, Mon/Wed/Fri,
    9am-2pm" — that RecurringSchedule rows expand against. One row per
    day-of-week (so a "Mon/Wed/Fri" pattern is 3 ShiftTemplate rows), same
    convention as EmployeeAvailability, rather than packing multiple days
    into one row.
    """

    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="shift_templates")
    location = models.ForeignKey(
        BusinessLocation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shift_templates",
        help_text="Optional. Null applies business-wide.",
    )
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="shift_templates")
    name = models.CharField(max_length=255, blank=True, default="")
    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    start_time = models.TimeField()
    end_time = models.TimeField(help_text="If <= start_time, the shift is treated as crossing midnight.")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["business", "day_of_week", "start_time"]

    def __str__(self):
        return f"{self.name or self.position.name} ({self.get_day_of_week_display()} {self.start_time}-{self.end_time})"


class RecurringSchedule(models.Model):
    """
    "This membership works this ShiftTemplate, weekly, starting Jan 1" —
    the durable rule that employees.scheduling expands into real
    EmployeeShift rows. The rule always references a ShiftTemplate (rather
    than also supporting freeform custom start/end times directly on the
    schedule) to keep the expansion logic in one place; a one-off shift
    that doesn't fit any template is just created directly as an
    EmployeeShift with recurring_schedule=None.
    """

    class Recurrence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="recurring_schedules"
    )
    shift_template = models.ForeignKey(
        ShiftTemplate, on_delete=models.PROTECT, related_name="recurring_schedules"
    )
    recurrence_rule = models.CharField(max_length=16, choices=Recurrence.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Null means ongoing, no end date.")
    is_active = models.BooleanField(
        default=True, help_text="Pausing this stops future expansion without deleting history."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["membership", "start_date"]

    @property
    def business(self):
        return self.membership.business

    def __str__(self):
        return f"{self.membership} – {self.shift_template} ({self.recurrence_rule})"


class EmployeeShift(models.Model):
    """
    A real, persisted shift — replaces employee_shifts. Either generated by
    employees.tasks.expand_recurring_schedules from a RecurringSchedule
    (`recurring_schedule` set), or created directly as a one-off
    (`recurring_schedule` null).
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(BusinessMembership, on_delete=models.CASCADE, related_name="shifts")
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="shifts")
    recurring_schedule = models.ForeignKey(
        RecurringSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_shifts",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_at"]
        constraints = [
            # Backs the idempotency of employees.tasks.expand_recurring_schedules:
            # running it twice for the same schedule + occurrence date must not
            # create a duplicate shift, even under concurrent Beat runs.
            models.UniqueConstraint(
                fields=["recurring_schedule", "start_at"],
                condition=Q(recurring_schedule__isnull=False),
                name="unique_shift_per_recurring_schedule_occurrence",
            ),
        ]

    @property
    def business(self):
        return self.membership.business

    def __str__(self):
        return f"{self.membership} {self.start_at:%Y-%m-%d %H:%M}-{self.end_at:%H:%M}"


class ShiftSwapRequest(models.Model):
    """
    A request to give away one shift, either to a specific membership or
    left open (`target_membership=None`) for a manager to assign on
    approval. Not a true two-way trade (the model only carries one shift) —
    matches what was actually built in the old frontend; a real
    shift-for-shift trade would need a second shift FK and is left for a
    future iteration if needed.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shift = models.ForeignKey(EmployeeShift, on_delete=models.CASCADE, related_name="swap_requests")
    requesting_membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="shift_swap_requests_made"
    )
    target_membership = models.ForeignKey(
        BusinessMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shift_swap_requests_targeted",
        help_text="Who the requester wants to swap with. Null means an open request.",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        BusinessMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shift_swaps_approved",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def business(self):
        return self.requesting_membership.business

    def __str__(self):
        return f"Swap request for {self.shift} by {self.requesting_membership} ({self.status})"


class TimeOffRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        BusinessMembership, on_delete=models.CASCADE, related_name="time_off_requests"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        BusinessMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="time_off_approvals",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                check=Q(end_date__gte=models.F("start_date")), name="time_off_end_date_after_start_date"
            ),
        ]

    @property
    def business(self):
        return self.membership.business

    def __str__(self):
        return f"{self.membership} off {self.start_date}–{self.end_date} ({self.status})"


class PayStub(models.Model):
    """
    A generated pay stub. Hours are pulled from actual TimeEntry rows by
    employees.payroll.generate_pay_stub, never entered manually.
    `breakdown` stores the full calculation (per-week regular/overtime
    split, rates, tax) so the final numbers are always inspectable rather
    than trusted blindly — see employees/payroll.py for the (placeholder)
    tax logic disclaimer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(BusinessMembership, on_delete=models.CASCADE, related_name="pay_stubs")
    position = models.ForeignKey(
        Position,
        on_delete=models.PROTECT,
        related_name="pay_stubs",
        help_text="Which Position's hourly_rate this pay stub was calculated from.",
    )
    pay_period_start = models.DateField()
    pay_period_end = models.DateField()
    regular_hours = models.DecimalField(max_digits=6, decimal_places=2)
    overtime_hours = models.DecimalField(max_digits=6, decimal_places=2)
    gross_pay = models.DecimalField(max_digits=10, decimal_places=2)
    net_pay = models.DecimalField(max_digits=10, decimal_places=2)
    breakdown = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-pay_period_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "pay_period_start", "pay_period_end"],
                name="unique_pay_stub_per_membership_period",
            ),
        ]

    @property
    def business(self):
        return self.membership.business

    def __str__(self):
        return f"{self.membership} {self.pay_period_start}–{self.pay_period_end}"
