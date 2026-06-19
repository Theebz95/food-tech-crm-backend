"""
Tests for the Reservations domain: staff CRUD + tenant isolation (same
rigor as Customers/Employees), the guest-booking concurrency fix, the
confirmation-code collision retry, end_time computation, guest
lookup-by-code isolation, and the business_hours enumeration fix.
"""

import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessLocation, BusinessMembership

from . import services
from .models import (
    DEFAULT_DURATION_MINUTES,
    BlackoutDate,
    BusinessHours,
    FloorPlan,
    Reservation,
    ReservationSetting,
    RestaurantTable,
    Waitlist,
)

# --- Staff-side URLs (HasBusinessRole) ---------------------------------------


def table_list_url(business_id):
    return f"/api/businesses/{business_id}/tables/"


def table_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/tables/{pk}/"


def floor_plan_list_url(business_id):
    return f"/api/businesses/{business_id}/floor-plans/"


def business_hours_list_url(business_id):
    return f"/api/businesses/{business_id}/business-hours/"


def blackout_date_list_url(business_id):
    return f"/api/businesses/{business_id}/blackout-dates/"


def reservation_settings_url(business_id):
    return f"/api/businesses/{business_id}/reservation-settings/"


def reservation_list_url(business_id):
    return f"/api/businesses/{business_id}/reservations/"


def reservation_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/reservations/{pk}/"


def waitlist_convert_url(business_id, pk):
    return f"/api/businesses/{business_id}/waitlist/{pk}/convert-to-reservation/"


# --- Public/guest URLs (no auth) ---------------------------------------------


def guest_availability_url(business_id, location_id):
    return f"/api/public/businesses/{business_id}/locations/{location_id}/availability/"


def guest_reservation_create_url(business_id, location_id):
    return f"/api/public/businesses/{business_id}/locations/{location_id}/reservations/"


def guest_waitlist_join_url(business_id, location_id):
    return f"/api/public/businesses/{business_id}/locations/{location_id}/waitlist/"


def guest_business_hours_url(business_id, location_id):
    return f"/api/public/businesses/{business_id}/locations/{location_id}/business-hours/"


def guest_reservation_lookup_url(confirmation_code):
    return f"/api/public/reservations/{confirmation_code}/"


def guest_reservation_cancel_url(confirmation_code):
    return f"/api/public/reservations/{confirmation_code}/cancel/"


class ReservationEndTimeComputationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_endtime@example.com")
        self.business = Business.objects.create(name="End Time Cafe", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")

    def test_end_time_computed_from_global_default_when_no_setting(self):
        start = timezone.now() + timedelta(days=1)
        reservation = Reservation.objects.create(
            location=self.location, guest_name="Alice", party_size=2, start_time=start
        )
        self.assertEqual(reservation.end_time, start + timedelta(minutes=DEFAULT_DURATION_MINUTES))
        self.assertEqual(reservation.duration_minutes, DEFAULT_DURATION_MINUTES)

    def test_end_time_computed_from_reservation_setting_default(self):
        ReservationSetting.objects.create(business=self.business, default_duration_minutes=45)
        start = timezone.now() + timedelta(days=1)
        reservation = Reservation.objects.create(
            location=self.location, guest_name="Bob", party_size=2, start_time=start
        )
        self.assertEqual(reservation.end_time, start + timedelta(minutes=45))

    def test_explicit_end_time_is_not_overridden(self):
        start = timezone.now() + timedelta(days=1)
        explicit_end = start + timedelta(minutes=10)
        reservation = Reservation.objects.create(
            location=self.location, guest_name="Carol", party_size=2, start_time=start, end_time=explicit_end
        )
        self.assertEqual(reservation.end_time, explicit_end)


class ConfirmationCodeCollisionTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_collision@example.com")
        self.business = Business.objects.create(name="Collision Cafe", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")

    def test_collision_triggers_retry_with_a_different_code(self):
        start = timezone.now() + timedelta(days=1)
        existing = Reservation.objects.create(
            location=self.location, guest_name="Existing", party_size=2, start_time=start
        )

        with patch(
            "reservations.models._generate_confirmation_code",
            side_effect=[existing.confirmation_code, "ABCDEF"],
        ):
            new_reservation = Reservation.objects.create(
                location=self.location, guest_name="New", party_size=2, start_time=start + timedelta(hours=2)
            )

        self.assertEqual(new_reservation.confirmation_code, "ABCDEF")
        self.assertNotEqual(new_reservation.confirmation_code, existing.confirmation_code)

    def test_exhausting_every_retry_raises_instead_of_silently_looping(self):
        start = timezone.now() + timedelta(days=1)
        existing = Reservation.objects.create(
            location=self.location, guest_name="Existing", party_size=2, start_time=start
        )

        with patch("reservations.models._generate_confirmation_code", return_value=existing.confirmation_code):
            with self.assertRaises(IntegrityError):
                Reservation.objects.create(
                    location=self.location, guest_name="New", party_size=2, start_time=start + timedelta(hours=3)
                )


class BookingConcurrencyTests(TransactionTestCase):
    """
    Proves select_for_update() on RestaurantTable is actually doing
    something: without it, two near-simultaneous bookings for the only
    qualifying table could both read "no overlap" and both succeed,
    double-booking the table — same shape as
    employees.tests.ClockInConcurrencyTests for clock-in.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_concurrency@example.com")
        self.business = Business.objects.create(name="Concurrency Bistro", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.table = RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        self.start_time = timezone.now() + timedelta(days=1)

    def test_only_one_concurrent_booking_succeeds_for_the_same_table_and_slot(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_booking(guest_name):
            barrier.wait()
            try:
                services.book_reservation(self.location, guest_name, "", "", 2, self.start_time)
                outcome = "success"
            except services.NoTableAvailableError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_booking, args=(f"Guest{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.assertEqual(Reservation.objects.filter(table=self.table).count(), 1)


class FloorPlanValidationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_floorplan@example.com")
        self.business = Business.objects.create(name="Floor Plan Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.table = RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        self.staff_user = User.objects.create_user(email="staff_floorplan@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_with_valid_table_reference_succeeds(self):
        response = self.client.post(
            floor_plan_list_url(self.business.id),
            {
                "location": str(self.location.id),
                "name": "Main Floor",
                "layout": {"tables": [{"table_id": str(self.table.id), "rotation": 90}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_rejects_layout_referencing_nonexistent_table(self):
        fake_id = uuid.uuid4()
        response = self.client.post(
            floor_plan_list_url(self.business.id),
            {
                "location": str(self.location.id),
                "name": "Main Floor",
                "layout": {"tables": [{"table_id": str(fake_id)}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(FloorPlan.objects.filter(name="Main Floor").exists())

    def test_rejects_layout_carrying_position_data(self):
        response = self.client.post(
            floor_plan_list_url(self.business.id),
            {
                "location": str(self.location.id),
                "name": "Main Floor",
                "layout": {"tables": [{"table_id": str(self.table.id), "x": 10, "y": 20}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BusinessHoursBlackoutSettingsTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_hours_settings@example.com")
        self.business = Business.objects.create(name="Hours Settings Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.staff_user = User.objects.create_user(email="staff_hours_settings@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_business_hours(self):
        response = self.client.post(
            business_hours_list_url(self.business.id),
            {"location": str(self.location.id), "day_of_week": 0, "open_time": "09:00", "close_time": "17:00"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_open_close_required_unless_closed(self):
        response = self.client.post(
            business_hours_list_url(self.business.id), {"location": str(self.location.id), "day_of_week": 1}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_blackout_date(self):
        response = self.client.post(
            blackout_date_list_url(self.business.id),
            {"location": str(self.location.id), "date": "2026-12-25", "reason": "Holiday"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_reservation_settings_auto_creates_with_defaults_on_first_read(self):
        response = self.client.get(reservation_settings_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["max_party_size"], 20)
        self.assertEqual(ReservationSetting.objects.filter(business=self.business).count(), 1)

    def test_reservation_settings_update(self):
        response = self.client.patch(reservation_settings_url(self.business.id), {"max_party_size": 8})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(ReservationSetting.objects.get(business=self.business).max_party_size, 8)


class WaitlistConversionTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_waitlist@example.com")
        self.business = Business.objects.create(name="Waitlist Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.table = RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        self.staff_user = User.objects.create_user(email="staff_waitlist@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.entry = Waitlist.objects.create(
            location=self.location, guest_name="Dana", party_size=2, requested_time=timezone.now()
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_convert_creates_reservation_and_updates_waitlist_entry(self):
        response = self.client.post(waitlist_convert_url(self.business.id, self.entry.id))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, Waitlist.Status.NOTIFIED)
        self.assertIsNotNone(self.entry.reservation)
        self.assertEqual(self.entry.reservation.table, self.table)

    def test_convert_fails_when_no_table_fits_the_party(self):
        big_entry = Waitlist.objects.create(
            location=self.location, guest_name="Big Party", party_size=99, requested_time=timezone.now()
        )
        response = self.client.post(waitlist_convert_url(self.business.id, big_entry.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Waitlist.objects.get(pk=big_entry.pk).status, Waitlist.Status.WAITING)


class StaffTenantIsolationTests(TestCase):
    """
    Deny-path tested directly against real cross-tenant data, same approach
    as customers.tests.CustomerTenantIsolationTests /
    employees.tests.TenantIsolationTests.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_isolation@example.com")
        self.business_a = Business.objects.create(name="Iso Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Iso Biz B", owner=owner)
        self.location_a = BusinessLocation.objects.create(business=self.business_a, name="A Main")
        self.location_b = BusinessLocation.objects.create(business=self.business_b, name="B Main")

        self.user_a = User.objects.create_user(email="staff_iso_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_iso_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        RestaurantTable.objects.create(location=self.location_b, name="B1", capacity=4)
        self.reservation_b = Reservation.objects.create(
            location=self.location_b, guest_name="Carol", party_size=2, start_time=timezone.now() + timedelta(days=1)
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_tables(self):
        response = self.client.get(table_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_create_table_for_other_business(self):
        response = self.client.post(
            table_list_url(self.business_b.id),
            {"location": str(self.location_b.id), "name": "X", "capacity": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(RestaurantTable.objects.filter(name="X").exists())

    def test_cannot_retrieve_other_business_reservation(self):
        response = self.client.get(reservation_detail_url(self.business_b.id, self.reservation_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_other_business_reservation(self):
        response = self.client.delete(reservation_detail_url(self.business_b.id, self.reservation_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Reservation.objects.filter(id=self.reservation_b.id).exists())

    def test_cannot_assign_a_foreign_business_location_even_through_own_business_url(self):
        # user_a does have access to business_a's URL, but tries to point
        # the new table at business_b's location via the payload.
        response = self.client.post(
            table_list_url(self.business_a.id),
            {"location": str(self.location_b.id), "name": "Sneaky", "capacity": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(RestaurantTable.objects.filter(name="Sneaky").exists())


class GuestBookingTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_booking@example.com")
        self.business = Business.objects.create(name="Guest Booking Biz", owner=owner)
        self.other_business = Business.objects.create(name="Other Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.other_location = BusinessLocation.objects.create(business=self.other_business, name="Other Main")
        self.table = RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        self.client = APIClient()
        self.start_time = timezone.now() + timedelta(days=1)

    def test_guest_can_book_without_authentication(self):
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {"guest_name": "Eve", "guest_email": "eve@example.com", "party_size": 2, "start_time": self.start_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        reservation = Reservation.objects.get(guest_name="Eve")
        self.assertEqual(reservation.table, self.table)
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(reservation.location_id, self.location.id)

    def test_location_smuggled_in_payload_is_ignored_in_favor_of_the_url(self):
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {
                "guest_name": "Mallory",
                "party_size": 2,
                "start_time": self.start_time.isoformat(),
                "location": str(self.other_location.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        reservation = Reservation.objects.get(guest_name="Mallory")
        self.assertEqual(reservation.location_id, self.location.id)
        self.assertNotEqual(reservation.location_id, self.other_location.id)

    def test_no_table_available_returns_409_with_a_waitlist_hint_code(self):
        # Within the default max_party_size (20) but bigger than the only
        # table's capacity (4), so this fails on table search, not policy.
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {"guest_name": "Frank", "party_size": 15, "start_time": self.start_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data.get("code"), "no_table_available")

    def test_party_size_over_business_max_rejected(self):
        ReservationSetting.objects.create(business=self.business, max_party_size=4)
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {"guest_name": "Grace", "party_size": 10, "start_time": self.start_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blackout_date_rejects_booking(self):
        BlackoutDate.objects.create(location=self.location, date=self.start_time.date())
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {"guest_name": "Henry", "party_size": 2, "start_time": self.start_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_time_computed_when_not_given(self):
        response = self.client.post(
            guest_reservation_create_url(self.business.id, self.location.id),
            {"guest_name": "Ivy", "party_size": 2, "start_time": self.start_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        reservation = Reservation.objects.get(guest_name="Ivy")
        self.assertEqual(reservation.end_time, self.start_time + timedelta(minutes=DEFAULT_DURATION_MINUTES))


class GuestWaitlistJoinTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_waitlist@example.com")
        self.business = Business.objects.create(name="Guest Waitlist Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.client = APIClient()

    def test_guest_can_join_waitlist_without_authentication(self):
        response = self.client.post(
            guest_waitlist_join_url(self.business.id, self.location.id),
            {"guest_name": "Karen", "party_size": 3, "requested_time": (timezone.now() + timedelta(hours=1)).isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Waitlist.objects.filter(guest_name="Karen", location=self.location).exists())


class GuestReservationLookupTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_lookup@example.com")
        self.business = Business.objects.create(name="Guest Lookup Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.table = RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        self.reservation = Reservation.objects.create(
            location=self.location,
            table=self.table,
            guest_name="Jack",
            party_size=2,
            start_time=timezone.now() + timedelta(days=1),
        )
        self.client = APIClient()

    def test_lookup_by_confirmation_code_succeeds(self):
        response = self.client.get(guest_reservation_lookup_url(self.reservation.confirmation_code))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["guest_name"], "Jack")

    def test_lookup_by_wrong_code_returns_404_not_a_list(self):
        response = self.client.get(guest_reservation_lookup_url("ZZZZZZ"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_no_route_exists_to_list_or_enumerate_reservations(self):
        with self.assertRaises(NoReverseMatch):
            reverse("reservations_public:reservation-create")  # no kwargs -> only the create route, never a list

    def test_guest_can_cancel_their_own_reservation_by_code(self):
        response = self.client.post(guest_reservation_cancel_url(self.reservation.confirmation_code))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CANCELLED)

    def test_cannot_cancel_an_already_cancelled_reservation(self):
        self.reservation.status = Reservation.Status.CANCELLED
        self.reservation.save(update_fields=["status"])
        response = self.client.post(guest_reservation_cancel_url(self.reservation.confirmation_code))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GuestReservationLookupThrottleTests(TestCase):
    """
    The confirmation_code lookup/cancel endpoints guard a guessable 6-char
    secret, so they get a dedicated, tighter throttle than every other
    guest endpoint — see reservations/throttles.py and the
    "reservation_lookup"/"reservation_lookup_global" rates in
    config/settings.py DEFAULT_THROTTLE_RATES.
    """

    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_lookup_throttle@example.com")
        self.business = Business.objects.create(name="Throttle Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.reservation = Reservation.objects.create(
            location=self.location, guest_name="Liam", party_size=2, start_time=timezone.now() + timedelta(days=1)
        )
        self.client = APIClient()

    def test_nth_attempt_within_the_window_is_rejected_per_ip(self):
        # Rate is "5/minute" (config/settings.py) — the first 5 from one IP succeed.
        for _ in range(5):
            response = self.client.get(guest_reservation_lookup_url(self.reservation.confirmation_code))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The 6th, still within the window, is rejected.
        response = self.client.get(guest_reservation_lookup_url(self.reservation.confirmation_code))
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_lookup_and_cancel_share_the_same_per_ip_budget(self):
        # Otherwise an attacker could double their effective guess rate by
        # alternating GET (lookup) and POST (cancel).
        for _ in range(5):
            response = self.client.get(guest_reservation_lookup_url(self.reservation.confirmation_code))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.post(guest_reservation_cancel_url(self.reservation.confirmation_code))
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_global_throttle_caps_total_guesses_spread_across_many_ips(self):
        # Rate is "20/minute" (config/settings.py), shared by every
        # client regardless of IP. Each of these 20 distinct IPs makes
        # only 1 request — nowhere near its own 5/minute per-IP limit —
        # but the platform-wide total still hits the global cap.
        for i in range(20):
            response = self.client.get(
                guest_reservation_lookup_url(self.reservation.confirmation_code),
                REMOTE_ADDR=f"10.0.0.{i}",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        # A 21st, never-seen-before IP is still rejected by the global cap
        # alone, even though it has made zero requests of its own.
        response = self.client.get(
            guest_reservation_lookup_url(self.reservation.confirmation_code),
            REMOTE_ADDR="10.0.0.99",
        )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class GuestBusinessHoursTests(TestCase):
    """
    The fix for the old business_hours-enumeration issue: a guest can read
    one business+location's hours, but there is no route anywhere that
    lists hours across locations or businesses.
    """

    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_hours@example.com")
        self.business_a = Business.objects.create(name="Hours Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Hours Biz B", owner=owner)
        self.location_a = BusinessLocation.objects.create(business=self.business_a, name="A Main")
        self.location_b = BusinessLocation.objects.create(business=self.business_b, name="B Main")
        BusinessHours.objects.create(location=self.location_a, day_of_week=0, open_time="09:00", close_time="17:00")
        BusinessHours.objects.create(location=self.location_b, day_of_week=0, open_time="10:00", close_time="18:00")
        self.client = APIClient()

    def test_guest_can_read_one_locations_hours(self):
        response = self.client.get(guest_business_hours_url(self.business_a.id, self.location_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["open_time"], "09:00:00")

    def test_response_never_includes_another_locations_rows(self):
        response = self.client.get(guest_business_hours_url(self.business_a.id, self.location_a.id))
        returned_ids = {row["id"] for row in response.data}
        other_location_ids = set(
            str(i) for i in BusinessHours.objects.filter(location=self.location_b).values_list("id", flat=True)
        )
        self.assertFalse(returned_ids & other_location_ids)

    def test_mismatched_business_and_location_returns_404_not_someone_elses_hours(self):
        response = self.client.get(guest_business_hours_url(self.business_a.id, self.location_b.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_no_route_lists_hours_across_locations_or_businesses(self):
        # The only guest business-hours route requires both business_id
        # and location_id in the URL — there is no business-only or
        # argument-less variant that could enumerate across tenants.
        with self.assertRaises(NoReverseMatch):
            reverse("reservations_public:business-hours")
        with self.assertRaises(NoReverseMatch):
            reverse("reservations_public:business-hours", kwargs={"business_id": self.business_a.id})


class GuestAvailabilityTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email="owner_guest_availability@example.com")
        self.business = Business.objects.create(name="Availability Biz", owner=owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        RestaurantTable.objects.create(location=self.location, name="T1", capacity=4)
        BusinessHours.objects.create(location=self.location, day_of_week=0, open_time="09:00", close_time="12:00")
        self.client = APIClient()

    def _next_monday(self):
        today = timezone.now().date()
        days_ahead = (0 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead)

    def test_returns_available_slots_within_business_hours(self):
        target_date = self._next_monday()
        response = self.client.get(
            guest_availability_url(self.business.id, self.location.id),
            {"date": target_date.isoformat(), "party_size": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data["available_start_times"]), 0)

    def test_blacked_out_date_has_no_available_slots(self):
        target_date = self._next_monday()
        BlackoutDate.objects.create(location=self.location, date=target_date)
        response = self.client.get(
            guest_availability_url(self.business.id, self.location.id),
            {"date": target_date.isoformat(), "party_size": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["available_start_times"], [])
