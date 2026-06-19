"""
Service-layer functions for time tracking + geofencing.

This is the one place distance/within-geofence verdicts are computed.
Views never accept a distance or a "within range" boolean from the client —
only raw lat/lng — and pass them through `verify_geofence` here. This is
the direct fix for the Phase 1 audit finding that the old frontend computed
and trusted this entirely client-side (src/lib/geolocation.ts).

State-transition functions (`clock_in`, `clock_out`, `start_break`,
`end_break`) are the only way TimeEntry/TimeEntryBreak rows get created or
closed — there is no generic create/update exposed via the API (see
views.py) — so the state machine invariants enforced here (no double
clock-in, no clock-out without an open entry, etc.) can't be bypassed by
hitting a CRUD endpoint directly.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from core.models import BusinessMembership

from .models import GeofenceSetting, LocationVerificationLog, TimeEntry, TimeEntryBreak

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
