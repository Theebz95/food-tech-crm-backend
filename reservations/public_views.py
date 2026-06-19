"""
Public, unauthenticated Reservations endpoints — guest availability check,
guest booking, guest reservation lookup-by-code, guest waitlist join, and a
guest-safe single-location business hours read.

These intentionally use a different permission model from every other view
in this codebase: there is no `User` and no `BusinessMembership` for a
walk-up guest making a reservation (the old guest-reservation Edge Function
never authenticated callers either — that's correct behavior to keep, not
a gap to close). `core.permissions.HasBusinessRole` simply doesn't apply
here; there's no membership to check against.

`authentication_classes = []` (not just `permission_classes = [AllowAny]`)
on every view below, same reasoning as `finance/webhooks.py`'s
`StripeWebhookView`: a guest request carries no Supabase JWT at all, so
there's nothing for `SupabaseAuthentication` to even attempt.

Since there's no authenticated-user throttle bucket to fall back on here,
rate limiting is the primary abuse defense (see README "Reservations
domain"). Each view gets its own `ScopedRateThrottle` scope, configured in
`config/settings.py` `DEFAULT_THROTTLE_RATES` — not just the shared global
"anon" bucket — so a burst against booking can't also exhaust the budget
for, say, the availability check, and vice versa.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.models import BusinessLocation

from . import services
from .models import BusinessHours, Reservation
from .serializers import (
    AvailabilityQuerySerializer,
    BusinessHoursSerializer,
    GuestReservationSerializer,
    GuestWaitlistSerializer,
)
from .throttles import GlobalReservationLookupThrottle


def _get_location(business_id, location_id):
    return get_object_or_404(BusinessLocation, id=location_id, business_id=business_id)


def _get_reservation_by_code(confirmation_code):
    return get_object_or_404(Reservation, confirmation_code=confirmation_code.upper())


class GuestAvailabilityView(APIView):
    """
    GET .../availability/?date=&party_size=&duration_minutes=
    Read-only: open start times for one location/date/party size — never
    an enumeration of other businesses or locations.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reservation_availability"

    def get(self, request, business_id=None, location_id=None):
        query = AvailabilityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        location = _get_location(business_id, location_id)
        slots = services.get_available_slots(
            location,
            query.validated_data["date"],
            query.validated_data["party_size"],
            query.validated_data.get("duration_minutes"),
        )
        return Response({"available_start_times": [slot.isoformat() for slot in slots]})


class GuestReservationCreateView(APIView):
    """POST .../reservations/ — create a guest booking. `location` comes from the URL, never the body."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reservation_booking"

    def post(self, request, business_id=None, location_id=None):
        location = _get_location(business_id, location_id)
        serializer = GuestReservationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            reservation = services.book_reservation(
                location=location,
                guest_name=data["guest_name"],
                guest_email=data.get("guest_email", ""),
                guest_phone=data.get("guest_phone", ""),
                party_size=data["party_size"],
                start_time=data["start_time"],
                duration_minutes=data.get("duration_minutes"),
            )
        except services.NoTableAvailableError as exc:
            # Distinct code so the frontend can offer to join the waitlist
            # instead, without us auto-enrolling the guest in one.
            return Response({"detail": str(exc), "code": "no_table_available"}, status=status.HTTP_409_CONFLICT)
        except services.BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(GuestReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)


class GuestReservationLookupView(APIView):
    """
    GET /api/public/reservations/<confirmation_code>/
    Exact-match lookup only — confirmation_code is the sole key, and there
    is no list endpoint anywhere in this app, so a guest can't enumerate
    other reservations by id, date, or business.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    # Two independent layers: ScopedRateThrottle caps any single IP;
    # GlobalReservationLookupThrottle caps the total guess rate across
    # every IP combined (see reservations/throttles.py). Both run on
    # every request — either one tripping rejects it.
    throttle_classes = [ScopedRateThrottle, GlobalReservationLookupThrottle]
    throttle_scope = "reservation_lookup"

    def get(self, request, confirmation_code=None):
        reservation = _get_reservation_by_code(confirmation_code)
        return Response(GuestReservationSerializer(reservation).data)


class GuestReservationCancelView(APIView):
    """POST /api/public/reservations/<confirmation_code>/cancel/ — guest-initiated cancellation."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Shares the same scope/budget as GuestReservationLookupView on
    # purpose — otherwise an attacker could double the effective guess
    # rate just by alternating GET and POST.
    throttle_classes = [ScopedRateThrottle, GlobalReservationLookupThrottle]
    throttle_scope = "reservation_lookup"

    def post(self, request, confirmation_code=None):
        reservation = _get_reservation_by_code(confirmation_code)
        if reservation.status in (Reservation.Status.COMPLETED, Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW):
            return Response(
                {"detail": f"Cannot cancel a reservation that is already {reservation.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status", "updated_at"])
        return Response(GuestReservationSerializer(reservation).data)


class GuestWaitlistJoinView(APIView):
    """POST .../waitlist/ — join the waitlist directly (not an automatic fallback from a failed booking)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reservation_waitlist"

    def post(self, request, business_id=None, location_id=None):
        location = _get_location(business_id, location_id)
        serializer = GuestWaitlistSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        entry = services.join_waitlist(
            location=location,
            guest_name=data["guest_name"],
            guest_email=data.get("guest_email", ""),
            guest_phone=data.get("guest_phone", ""),
            party_size=data["party_size"],
            requested_time=data["requested_time"],
        )
        return Response(GuestWaitlistSerializer(entry).data, status=status.HTTP_201_CREATED)


class GuestBusinessHoursView(APIView):
    """
    GET .../business-hours/
    The fix for the old business_hours-enumeration issue (Phase 1 audit
    finding #2): resolves hours for exactly the one business+location
    named in the URL. There is no route anywhere in this app that lists
    hours across locations or businesses.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reservation_business_hours"

    def get(self, request, business_id=None, location_id=None):
        location = _get_location(business_id, location_id)
        hours = BusinessHours.objects.filter(location=location)
        return Response(BusinessHoursSerializer(hours, many=True).data)
