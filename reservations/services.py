"""
Booking service layer.

The actual security/concurrency fix this module exists for (Phase 1 audit
finding): the old guest-reservation Edge Function had no concurrency
control at all, so two guests hitting "book" for the same table/slot at the
same moment could both succeed, double-booking the table.
`_assign_table_and_book` wraps candidate-table selection + Reservation
creation in `transaction.atomic()` + `select_for_update()` on the
`RestaurantTable` rows being considered, so a second concurrent attempt
blocks until the first commits, then re-checks overlap and (correctly)
finds no table free — same pattern as `employees/services.py:clock_in`
locking the membership row before checking for an existing open TimeEntry.

Every reservation that goes through this module is the only path that
creates one with a table assigned — there is no generic "create a
Reservation with whatever table you like" endpoint (see views.py /
public_views.py), so this is the one place double-booking could be
reintroduced.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import BlackoutDate, BusinessHours, Reservation, ReservationSetting, RestaurantTable, Waitlist


class BookingError(Exception):
    """Base for all booking/waitlist errors. Views translate these to 400s."""


class BlackoutDateError(BookingError):
    pass


class OutsideBookingWindowError(BookingError):
    pass


class PartySizeTooLargeError(BookingError):
    pass


class NoTableAvailableError(BookingError):
    pass


class WaitlistError(BookingError):
    pass


@dataclass(frozen=True)
class _DefaultReservationSettings:
    """Used when a business hasn't created a ReservationSetting row yet."""

    default_duration_minutes: int = 90
    slot_interval_minutes: int = 30
    buffer_minutes: int = 0
    min_advance_minutes: int = 0
    max_advance_days: int = 60
    max_party_size: int = 20


def get_settings(business):
    return ReservationSetting.objects.filter(business=business).first() or _DefaultReservationSettings()


def _validate_guest_booking_request(location, start_time, party_size):
    """
    Policy checks that apply to a guest booking ahead of time, but not to a
    staff-initiated conversion of an existing waitlist entry (which is
    typically "seat this walk-in right now" — see convert_waitlist_entry).
    """
    settings = get_settings(location.business)

    if party_size > settings.max_party_size:
        raise PartySizeTooLargeError(f"Party size exceeds the maximum of {settings.max_party_size}.")

    now = timezone.now()
    if start_time < now + timedelta(minutes=settings.min_advance_minutes):
        raise OutsideBookingWindowError("Reservation start time is too soon.")
    if start_time > now + timedelta(days=settings.max_advance_days):
        raise OutsideBookingWindowError("Reservation start time is too far in advance.")

    if BlackoutDate.objects.filter(location=location, date=start_time.date()).exists():
        raise BlackoutDateError("This date is not available for reservations.")

    return settings


def _assign_table_and_book(
    location, guest_name, guest_email, guest_phone, party_size, start_time, duration_minutes, status, settings=None
):
    settings = settings or get_settings(location.business)
    end_time = start_time + timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=settings.buffer_minutes)

    with transaction.atomic():
        candidate_tables = (
            RestaurantTable.objects.select_for_update()
            .filter(location=location, is_active=True, capacity__gte=party_size)
            .order_by("capacity", "name")
        )
        assigned_table = None
        for table in candidate_tables:
            overlaps = Reservation.objects.filter(
                table=table,
                status__in=Reservation.ACTIVE_STATUSES,
                start_time__lt=end_time + buffer,
                end_time__gt=start_time - buffer,
            ).exists()
            if not overlaps:
                assigned_table = table
                break

        if assigned_table is None:
            raise NoTableAvailableError("No table is available for that time and party size.")

        return Reservation.objects.create(
            location=location,
            table=assigned_table,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            party_size=party_size,
            start_time=start_time,
            duration_minutes=duration_minutes,
            status=status,
        )


def book_reservation(
    location, guest_name, guest_email, guest_phone, party_size, start_time, duration_minutes=None
) -> Reservation:
    settings = _validate_guest_booking_request(location, start_time, party_size)
    duration = duration_minutes or settings.default_duration_minutes
    return _assign_table_and_book(
        location,
        guest_name,
        guest_email,
        guest_phone,
        party_size,
        start_time,
        duration,
        Reservation.Status.CONFIRMED,
        settings=settings,
    )


def join_waitlist(location, guest_name, guest_email, guest_phone, party_size, requested_time) -> Waitlist:
    return Waitlist.objects.create(
        location=location,
        guest_name=guest_name,
        guest_email=guest_email,
        guest_phone=guest_phone,
        party_size=party_size,
        requested_time=requested_time,
    )


def convert_waitlist_entry(entry: Waitlist, start_time=None, duration_minutes=None) -> Reservation:
    """
    Manager action: assign a real table to a waiting guest now. Skips the
    advance-booking-window check in `_validate_guest_booking_request` since
    this is typically "seat this walk-in" rather than booking ahead — but
    still goes through the same locking/overlap logic, so it can't
    double-book a table either.
    """
    with transaction.atomic():
        locked = Waitlist.objects.select_for_update().get(pk=entry.pk)
        if locked.status != Waitlist.Status.WAITING:
            raise WaitlistError("Waitlist entry is not waiting.")

        settings = get_settings(locked.location.business)
        duration = duration_minutes or settings.default_duration_minutes
        reservation = _assign_table_and_book(
            locked.location,
            locked.guest_name,
            locked.guest_email,
            locked.guest_phone,
            locked.party_size,
            start_time or locked.requested_time,
            duration,
            Reservation.Status.CONFIRMED,
            settings=settings,
        )

        locked.status = Waitlist.Status.NOTIFIED
        locked.reservation = reservation
        locked.save(update_fields=["status", "reservation", "updated_at"])
        return reservation


def get_available_slots(location, date, party_size, duration_minutes=None) -> list:
    """
    Available reservation start times (timezone-aware datetimes) for one
    location/date/party size, derived from BusinessHours, BlackoutDate, and
    actual table occupancy — never an old client-side computation.
    """
    settings = get_settings(location.business)
    duration = duration_minutes or settings.default_duration_minutes

    if BlackoutDate.objects.filter(location=location, date=date).exists():
        return []

    hours = BusinessHours.objects.filter(location=location, day_of_week=date.weekday()).first()
    if hours is None or hours.is_closed or not hours.open_time or not hours.close_time:
        return []

    tables = list(RestaurantTable.objects.filter(location=location, is_active=True, capacity__gte=party_size))
    if not tables:
        return []

    current_tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(datetime.combine(date, hours.open_time), current_tz)
    day_close = timezone.make_aware(datetime.combine(date, hours.close_time), current_tz)
    slot_interval = timedelta(minutes=settings.slot_interval_minutes)
    duration_td = timedelta(minutes=duration)
    buffer = timedelta(minutes=settings.buffer_minutes)

    now = timezone.now()
    min_start = now + timedelta(minutes=settings.min_advance_minutes)
    max_start = now + timedelta(days=settings.max_advance_days)

    # One query for the whole day rather than one per candidate slot.
    reservations = list(
        Reservation.objects.filter(
            table__in=tables,
            status__in=Reservation.ACTIVE_STATUSES,
            start_time__lt=day_close + buffer,
            end_time__gt=day_start - buffer,
        ).values("table_id", "start_time", "end_time")
    )

    available_slots = []
    slot_start = day_start
    while slot_start + duration_td <= day_close:
        slot_end = slot_start + duration_td
        if min_start <= slot_start <= max_start:
            for table in tables:
                conflict = any(
                    r["table_id"] == table.id and r["start_time"] < slot_end + buffer and r["end_time"] > slot_start - buffer
                    for r in reservations
                )
                if not conflict:
                    available_slots.append(slot_start)
                    break
        slot_start += slot_interval

    return available_slots
