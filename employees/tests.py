"""
Tests for the time tracking + geofencing security fix.

These exercise the full DRF stack (APIClient, real Postgres via Django's
test DB) rather than calling employees.services directly, except for the
concurrency test, which needs raw threads with their own DB connections to
actually exercise select_for_update() — APIClient calls in the same test
share one connection/transaction and wouldn't contend with each other.
"""

import threading
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership

from . import services
from .models import GeofenceSetting, LocationVerificationLog, TimeEntry

# Center point for all geofence tests. The "outside" point below is ~111km
# away (1 degree of latitude), comfortably outside any reasonable radius.
CENTER_LAT = Decimal("40.000000")
CENTER_LNG = Decimal("-75.000000")
OUTSIDE_LAT = Decimal("41.000000")
RADIUS_METERS = 100


def time_entries_url(business_id):
    return f"/api/businesses/{business_id}/time-entries/"


def clock_in_url(business_id):
    return f"/api/businesses/{business_id}/time-entries/clock-in/"


def clock_out_url(business_id):
    return f"/api/businesses/{business_id}/time-entries/clock-out/"


def break_start_url(business_id):
    return f"/api/businesses/{business_id}/time-entries/break-start/"


def break_end_url(business_id):
    return f"/api/businesses/{business_id}/time-entries/break-end/"


class GeofenceVerificationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com")
        self.business = Business.objects.create(name="Test Cafe", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        GeofenceSetting.objects.create(
            business=self.business,
            center_latitude=CENTER_LAT,
            center_longitude=CENTER_LNG,
            radius_meters=RADIUS_METERS,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_clock_in_within_geofence_succeeds(self):
        response = self.client.post(
            clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        entry = TimeEntry.objects.get(membership=self.membership)
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_IN)
        self.assertTrue(entry.clock_in_within_geofence)
        self.assertEqual(entry.clock_in_distance_meters, Decimal("0.00"))

    def test_clock_in_outside_geofence_rejected(self):
        response = self.client.post(
            clock_in_url(self.business.id), {"latitude": OUTSIDE_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(TimeEntry.objects.filter(membership=self.membership).exists())

        # The rejection must still be logged for audit, with a correctly
        # computed (not client-trusted) distance.
        log = LocationVerificationLog.objects.get(membership=self.membership)
        self.assertFalse(log.passed)
        self.assertFalse(log.within_geofence)
        expected_distance = services.haversine_distance_meters(CENTER_LAT, CENTER_LNG, OUTSIDE_LAT, CENTER_LNG)
        self.assertAlmostEqual(float(log.distance_meters), float(expected_distance), delta=1)
        self.assertGreater(log.distance_meters, RADIUS_METERS)

    def test_double_clock_in_rejected(self):
        first = self.client.post(clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG})
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client.post(clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG})
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TimeEntry.objects.filter(membership=self.membership).count(), 1)

    def test_clock_out_without_open_entry_rejected(self):
        response = self.client.post(
            clock_out_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_clock_out_success(self):
        self.client.post(clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG})
        response = self.client.post(
            clock_out_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry = TimeEntry.objects.get(membership=self.membership)
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_OUT)
        self.assertIsNotNone(entry.clock_out_at)
        self.assertTrue(entry.clock_out_within_geofence)

    def test_clock_out_outside_geofence_rejected_and_stays_open(self):
        self.client.post(clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG})
        response = self.client.post(
            clock_out_url(self.business.id), {"latitude": OUTSIDE_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        entry = TimeEntry.objects.get(membership=self.membership)
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_IN)
        self.assertIsNone(entry.clock_out_at)

    def test_break_start_without_clock_in_rejected(self):
        response = self.client.post(break_start_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_break_start_and_end_success(self):
        self.client.post(clock_in_url(self.business.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG})

        start_response = self.client.post(break_start_url(self.business.id))
        self.assertEqual(start_response.status_code, status.HTTP_201_CREATED, start_response.data)
        self.assertEqual(len(start_response.data["breaks"]), 1)
        self.assertIsNone(start_response.data["breaks"][0]["break_end_at"])

        # Can't start a second break while one is open.
        second_start = self.client.post(break_start_url(self.business.id))
        self.assertEqual(second_start.status_code, status.HTTP_400_BAD_REQUEST)

        end_response = self.client.post(break_end_url(self.business.id))
        self.assertEqual(end_response.status_code, status.HTTP_200_OK, end_response.data)
        self.assertIsNotNone(end_response.data["breaks"][0]["break_end_at"])

        # Can't end a break that's already closed.
        second_end = self.client.post(break_end_url(self.business.id))
        self.assertEqual(second_end.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_geofence_configured_allows_clock_in_without_location_check(self):
        GeofenceSetting.objects.all().delete()
        response = self.client.post(
            clock_in_url(self.business.id), {"latitude": OUTSIDE_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        entry = TimeEntry.objects.get(membership=self.membership)
        self.assertIsNone(entry.clock_in_within_geofence)
        self.assertIsNone(entry.clock_in_distance_meters)

    def test_disabled_geofence_is_not_enforced(self):
        GeofenceSetting.objects.update(enabled=False)
        response = self.client.post(
            clock_in_url(self.business.id), {"latitude": OUTSIDE_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class TenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner2@example.com")
        self.business_a = Business.objects.create(name="Business A", owner=owner)
        self.business_b = Business.objects.create(name="Business B", owner=owner)

        self.user_a = User.objects.create_user(email="staffa@example.com")
        self.membership_a = BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )

        other_user_b = User.objects.create_user(email="staffb@example.com")
        self.membership_b = BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )
        TimeEntry.objects.create(membership=self.membership_b, clock_in_at=timezone.now())

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_time_entries(self):
        response = self.client.get(time_entries_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_clock_in_to_other_business(self):
        response = self.client.post(
            clock_in_url(self.business_b.id), {"latitude": CENTER_LAT, "longitude": CENTER_LNG}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TimeEntry.objects.filter(membership=self.membership_a).exists())

    def test_own_business_list_only_shows_own_business_entries(self):
        TimeEntry.objects.create(membership=self.membership_a, clock_in_at=timezone.now())
        response = self.client.get(time_entries_url(self.business_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


class ClockInConcurrencyTests(TransactionTestCase):
    """
    Proves select_for_update() is actually doing something: without locking
    the membership row, two near-simultaneous clock-ins could both read
    "no open entry exists" and both succeed, creating two open TimeEntry
    rows for the same membership (which the DB constraint would also catch,
    but the point here is the application-level race, not just the
    fallback DB constraint).
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner3@example.com")
        self.business = Business.objects.create(name="Concurrency Co", owner=owner)
        user = User.objects.create_user(email="staffc@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )

    def test_only_one_concurrent_clock_in_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_clock_in():
            barrier.wait()
            try:
                services.clock_in(self.membership, CENTER_LAT, CENTER_LNG)
                outcome = "success"
            except services.AlreadyClockedInError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_clock_in) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.assertEqual(TimeEntry.objects.filter(membership=self.membership).count(), 1)
