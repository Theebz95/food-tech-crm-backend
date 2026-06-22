"""
Service-layer state-machine functions: time tracking + geofencing, shift
swap approval, and time-off approval. (Recurring schedule expansion lives
in scheduling.py/tasks.py; pay stub calculation lives in payroll.py —
split out since they're a different kind of operation, not a request/response
state transition.)

Time tracking: this is the one place distance/within-geofence verdicts are
computed. Views never accept a distance or a "within range" boolean from
the client — only raw lat/lng — and pass them through `verify_geofence`
here. This is the direct fix for the Phase 1 audit finding that the old
frontend computed and trusted this entirely client-side
(src/lib/geolocation.ts).

Every function here is the only way its respective rows get created,
closed, or change status — there is no generic create/update exposed via
the API (see views.py) — so the state machine invariants enforced here (no
double clock-in, no approving a non-pending request, etc.) can't be
bypassed by hitting a CRUD endpoint directly.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import BusinessMembership

from .models import (
    EmployeeShift,
    GeofenceSetting,
    LocationVerificationLog,
    ShiftSwapRequest,
    TimeEntry,
    TimeEntryBreak,
    TimeOffRequest,
)

EARTH_RADIUS_METERS = 6_371_000


class TimeTrackingError(Exception):
    """Base for all domain errors raised by this module. Views translate these to 400s."""


class AlreadyClockedInError(TimeTrackingError):
    pass


class NoOpenTimeEntryError(TimeTrackingError):
    pass


class GeofenceViolationError(TimeTrackingError):
    def __init__(self, distance_meters, radius_meters):
        self.distance_meters = distance_meters
        self.radius_meters = radius_meters
        super().__init__(
            f"Location is {distance_meters:.1f}m from the allowed area "
            f"(radius {radius_meters}m)."
        )


class BreakStateError(TimeTrackingError):
    pass


@dataclass
class GeofenceCheckResult:
    setting: Optional[GeofenceSetting]
    distance_meters: Optional[Decimal]
    within_geofence: Optional[bool]
    passed: bool


def haversine_distance_meters(lat1, lng1, lat2, lng2) -> Decimal:
    """Great-circle distance between two lat/lng points, in meters."""
    lat1, lng1, lat2, lng2 = (float(lat1), float(lng1), float(lat2), float(lng2))
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return Decimal(EARTH_RADIUS_METERS * c).quantize(Decimal("0.01"))


def get_applicable_geofence(membership: BusinessMembership) -> Optional[GeofenceSetting]:
    """
    Location-specific setting takes precedence over the business-wide one.
    Disabled settings are treated the same as "none configured".
    """
    if membership.location_id:
        location_setting = GeofenceSetting.objects.filter(
            business_id=membership.business_id, location_id=membership.location_id, enabled=True
        ).first()
        if location_setting is not None:
            return location_setting

    return GeofenceSetting.objects.filter(
        business_id=membership.business_id, location__isnull=True, enabled=True
    ).first()


def verify_geofence(membership: BusinessMembership, latitude, longitude) -> GeofenceCheckResult:
    """
    Computes the actual distance and within-radius verdict server-side.
    If no enabled GeofenceSetting applies to this membership, there is
    nothing to enforce, so the check passes by default.
    """
    setting = get_applicable_geofence(membership)
    if setting is None:
        return GeofenceCheckResult(setting=None, distance_meters=None, within_geofence=None, passed=True)

    if latitude is None or longitude is None:
        # Geofencing is enabled but the client didn't report a location —
        # can't verify, so treat as outside the geofence rather than
        # silently letting it through.
        return GeofenceCheckResult(setting=setting, distance_meters=None, within_geofence=False, passed=False)

    distance = haversine_distance_meters(setting.center_latitude, setting.center_longitude, latitude, longitude)
    within = distance <= setting.radius_meters
    return GeofenceCheckResult(setting=setting, distance_meters=distance, within_geofence=within, passed=within)


def _log_check(membership, check_type, latitude, longitude, result: GeofenceCheckResult, time_entry=None):
    return LocationVerificationLog.objects.create(
        membership=membership,
        time_entry=time_entry,
        geofence_setting=result.setting,
        check_type=check_type,
        reported_latitude=latitude,
        reported_longitude=longitude,
        distance_meters=result.distance_meters,
        within_geofence=result.within_geofence,
        passed=result.passed,
    )


def clock_in(membership: BusinessMembership, latitude, longitude) -> TimeEntry:
    """
    Geofence violations are a hard block for now: the request is rejected
    outright rather than allowed-through-but-flagged. TODO: revisit once
    there's a business-level setting to choose hard-block vs flag-for-review
    (see README "Geofencing: hard block vs flag" for the tradeoff).
    """
    with transaction.atomic():
        # No open TimeEntry exists yet for a fresh clock-in, so there is no
        # TimeEntry row to lock. Lock the membership row instead — every
        # concurrent clock-in attempt for this membership has to acquire
        # this same lock first, which is what actually serializes them.
        locked_membership = BusinessMembership.objects.select_for_update().get(pk=membership.pk)

        if TimeEntry.objects.filter(membership=locked_membership, status=TimeEntry.Status.CLOCKED_IN).exists():
            raise AlreadyClockedInError("Already clocked in.")

        result = verify_geofence(locked_membership, latitude, longitude)
        log = _log_check(locked_membership, LocationVerificationLog.CheckType.CLOCK_IN, latitude, longitude, result)

        entry = None
        if result.passed:
            entry = TimeEntry.objects.create(
                membership=locked_membership,
                clock_in_at=timezone.now(),
                status=TimeEntry.Status.CLOCKED_IN,
                clock_in_lat=latitude,
                clock_in_lng=longitude,
                clock_in_distance_meters=result.distance_meters,
                clock_in_within_geofence=result.within_geofence,
            )
            log.time_entry = entry
            log.save(update_fields=["time_entry"])

    # Raised outside the atomic block on purpose: the log write above must
    # commit even on rejection (it's the audit trail for the rejection
    # itself), but raising from inside transaction.atomic() rolls back
    # everything written in that block, including the log.
    if not result.passed:
        raise GeofenceViolationError(
            result.distance_meters if result.distance_meters is not None else Decimal("0"),
            result.setting.radius_meters,
        )
    return entry


def clock_out(membership: BusinessMembership, latitude, longitude) -> TimeEntry:
    with transaction.atomic():
        entry = (
            TimeEntry.objects.select_for_update()
            .filter(membership=membership, status=TimeEntry.Status.CLOCKED_IN)
            .first()
        )
        if entry is None:
            raise NoOpenTimeEntryError("No open time entry to clock out of.")

        result = verify_geofence(membership, latitude, longitude)
        _log_check(membership, LocationVerificationLog.CheckType.CLOCK_OUT, latitude, longitude, result, entry)

        if result.passed:
            entry.clock_out_at = timezone.now()
            entry.status = TimeEntry.Status.CLOCKED_OUT
            entry.clock_out_lat = latitude
            entry.clock_out_lng = longitude
            entry.clock_out_distance_meters = result.distance_meters
            entry.clock_out_within_geofence = result.within_geofence
            entry.save(
                update_fields=[
                    "clock_out_at",
                    "status",
                    "clock_out_lat",
                    "clock_out_lng",
                    "clock_out_distance_meters",
                    "clock_out_within_geofence",
                    "updated_at",
                ]
            )

    # See clock_in() for why this is raised after the atomic block closes.
    if not result.passed:
        raise GeofenceViolationError(
            result.distance_meters if result.distance_meters is not None else Decimal("0"),
            result.setting.radius_meters,
        )
    return entry


def start_break(membership: BusinessMembership) -> TimeEntryBreak:
    with transaction.atomic():
        entry = (
            TimeEntry.objects.select_for_update()
            .filter(membership=membership, status=TimeEntry.Status.CLOCKED_IN)
            .first()
        )
        if entry is None:
            raise NoOpenTimeEntryError("Must be clocked in to start a break.")
        if entry.breaks.filter(break_end_at__isnull=True).exists():
            raise BreakStateError("A break is already in progress.")

        return TimeEntryBreak.objects.create(time_entry=entry, break_start_at=timezone.now())


def end_break(membership: BusinessMembership) -> TimeEntryBreak:
    with transaction.atomic():
        entry = (
            TimeEntry.objects.select_for_update()
            .filter(membership=membership, status=TimeEntry.Status.CLOCKED_IN)
            .first()
        )
        if entry is None:
            raise NoOpenTimeEntryError("Must be clocked in to end a break.")

        open_break = entry.breaks.filter(break_end_at__isnull=True).select_for_update().first()
        if open_break is None:
            raise BreakStateError("No break in progress to end.")

        open_break.break_end_at = timezone.now()
        open_break.save(update_fields=["break_end_at", "updated_at"])
        return open_break


class ShiftSwapError(Exception):
    pass


class TimeOffError(Exception):
    pass


def request_shift_swap(shift: EmployeeShift, requesting_membership: BusinessMembership, target_membership=None):
    if shift.membership_id != requesting_membership.id:
        raise ShiftSwapError("Can only request a swap for your own shift.")
    if target_membership is not None and target_membership.business_id != requesting_membership.business_id:
        raise ShiftSwapError("Target membership must belong to the same business.")
    if ShiftSwapRequest.objects.filter(shift=shift, status=ShiftSwapRequest.Status.PENDING).exists():
        raise ShiftSwapError("A swap request is already pending for this shift.")

    return ShiftSwapRequest.objects.create(
        shift=shift, requesting_membership=requesting_membership, target_membership=target_membership
    )


def approve_shift_swap(
    swap_request: ShiftSwapRequest, approving_membership: BusinessMembership, target_membership=None
) -> ShiftSwapRequest:
    """
    `target_membership` lets a manager resolve an open request (one with no
    target_membership set yet) at approval time; if the request already has
    one, this is optional and must match if provided.
    """
    with transaction.atomic():
        locked = ShiftSwapRequest.objects.select_for_update().get(pk=swap_request.pk)
        if locked.status != ShiftSwapRequest.Status.PENDING:
            raise ShiftSwapError("Swap request is not pending.")

        final_target = target_membership or locked.target_membership
        if final_target is None:
            raise ShiftSwapError("No target membership to assign this shift to.")
        if final_target.business_id != locked.requesting_membership.business_id:
            raise ShiftSwapError("Target membership must belong to the same business.")

        shift = EmployeeShift.objects.select_for_update().get(pk=locked.shift_id)
        shift.membership = final_target
        shift.save(update_fields=["membership", "updated_at"])

        locked.status = ShiftSwapRequest.Status.APPROVED
        locked.target_membership = final_target
        locked.approved_by = approving_membership
        locked.save(update_fields=["status", "target_membership", "approved_by", "updated_at"])
        return locked


def reject_shift_swap(swap_request: ShiftSwapRequest, approving_membership: BusinessMembership) -> ShiftSwapRequest:
    with transaction.atomic():
        locked = ShiftSwapRequest.objects.select_for_update().get(pk=swap_request.pk)
        if locked.status != ShiftSwapRequest.Status.PENDING:
            raise ShiftSwapError("Swap request is not pending.")

        locked.status = ShiftSwapRequest.Status.REJECTED
        locked.approved_by = approving_membership
        locked.save(update_fields=["status", "approved_by", "updated_at"])
        return locked


def approve_time_off(time_off_request: TimeOffRequest, approving_membership: BusinessMembership) -> TimeOffRequest:
    with transaction.atomic():
        locked = TimeOffRequest.objects.select_for_update().get(pk=time_off_request.pk)
        if locked.status != TimeOffRequest.Status.PENDING:
            raise TimeOffError("Time off request is not pending.")

        locked.status = TimeOffRequest.Status.APPROVED
        locked.approved_by = approving_membership
        locked.save(update_fields=["status", "approved_by", "updated_at"])
        return locked


def reject_time_off(time_off_request: TimeOffRequest, approving_membership: BusinessMembership) -> TimeOffRequest:
    with transaction.atomic():
        locked = TimeOffRequest.objects.select_for_update().get(pk=time_off_request.pk)
        if locked.status != TimeOffRequest.Status.PENDING:
            raise TimeOffError("Time off request is not pending.")

        locked.status = TimeOffRequest.Status.REJECTED
        locked.approved_by = approving_membership
        locked.save(update_fields=["status", "approved_by", "updated_at"])
        return locked


def handle_membership_deactivation(membership: BusinessMembership) -> None:
    """
    Auto-resolves everything left dangling when a BusinessMembership is
    deactivated — wired up via employees/signals.py (post_save on
    BusinessMembership, only on an is_active True->False transition), so
    this fires regardless of how deactivation happens: Django admin, a
    future API endpoint, or a one-off script. Idempotent by construction
    (every query below only ever touches still-open/still-pending rows),
    so re-running it against an already-deactivated membership is a no-op.

    - Any open TimeEntry is force-closed: clock_out_at = now(),
      auto_closed_on_deactivation = True. No geofence check runs (there's
      no client-reported location for an action nobody took) — this is
      explicitly not a real clock-out, which is exactly what the marker
      is for.
    - Any open TimeEntryBreak on that entry is force-closed the same way.
    - Any PENDING ShiftSwapRequest where this membership is the requester
      is CANCELLED (not REJECTED — nobody made a decision, the request
      just became moot).
    - Any PENDING ShiftSwapRequest where this membership is the *target*
      (someone wanted to swap with them) is also CANCELLED — the swap
      can't happen either way now.
    - Any PENDING TimeOffRequest by this membership is CANCELLED.
    """
    now = timezone.now()
    with transaction.atomic():
        # select_for_update() here because the rows get *read* (iterated)
        # before being updated, to find their breaks — the plain bulk
        # .update() calls below need no explicit lock of their own: an
        # UPDATE statement's WHERE-matched rows are already row-locked by
        # Postgres as part of the statement itself.
        open_entries = list(
            TimeEntry.objects.select_for_update().filter(membership=membership, status=TimeEntry.Status.CLOCKED_IN)
        )
        TimeEntryBreak.objects.filter(time_entry__in=open_entries, break_end_at__isnull=True).update(
            break_end_at=now, auto_closed_on_deactivation=True, updated_at=now
        )
        TimeEntry.objects.filter(pk__in=[e.pk for e in open_entries]).update(
            clock_out_at=now, status=TimeEntry.Status.CLOCKED_OUT, auto_closed_on_deactivation=True, updated_at=now
        )

        ShiftSwapRequest.objects.filter(status=ShiftSwapRequest.Status.PENDING).filter(
            Q(requesting_membership=membership) | Q(target_membership=membership)
        ).update(status=ShiftSwapRequest.Status.CANCELLED, updated_at=now)

        TimeOffRequest.objects.filter(membership=membership, status=TimeOffRequest.Status.PENDING).update(
            status=TimeOffRequest.Status.CANCELLED, updated_at=now
        )
