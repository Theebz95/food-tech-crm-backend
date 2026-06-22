"""
Decision 2 (from the cross-domain audit): deactivating a BusinessMembership
now auto-resolves everything left dangling, via a post_save signal
(employees/signals.py) — fires regardless of how deactivation happens.
See services.handle_membership_deactivation's docstring for the exact
rules. This is the dedicated, full-rigor test suite for that decision;
core/test_cross_domain_consistency.py keeps only the narrower
access-denial regression (a core.permissions concern).
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from authentication.models import User
from core.models import Business, BusinessMembership

from . import services
from .models import EmployeeShift, Position, ShiftSwapRequest, TimeEntry, TimeEntryBreak, TimeOffRequest


class MembershipDeactivationCascadeTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="deactivation-cascade-owner@example.com")
        self.business = Business.objects.create(name="Deactivation Cascade Biz", owner=owner)
        self.employee_user = User.objects.create_user(email="deactivation-cascade-employee@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=self.employee_user, role=BusinessMembership.Role.STAFF
        )
        self.other_user = User.objects.create_user(email="deactivation-cascade-other@example.com")
        self.other_membership = BusinessMembership.objects.create(
            business=self.business, user=self.other_user, role=BusinessMembership.Role.STAFF
        )
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))

    def _deactivate(self, membership):
        membership.is_active = False
        membership.save(update_fields=["is_active"])

    # --- TimeEntry / TimeEntryBreak ------------------------------------------------

    def test_open_time_entry_is_force_closed_on_deactivation(self):
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        self._deactivate(self.membership)

        entry.refresh_from_db()
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_OUT)
        self.assertIsNotNone(entry.clock_out_at)
        self.assertTrue(entry.auto_closed_on_deactivation)
        # No geofence check ran for this — never a real clock-out.
        self.assertIsNone(entry.clock_out_lat)
        self.assertIsNone(entry.clock_out_within_geofence)

    def test_open_break_on_an_open_entry_is_force_closed_too(self):
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        break_row = TimeEntryBreak.objects.create(time_entry=entry, break_start_at=timezone.now())

        self._deactivate(self.membership)

        break_row.refresh_from_db()
        self.assertIsNotNone(break_row.break_end_at)
        self.assertTrue(break_row.auto_closed_on_deactivation)

    def test_already_closed_time_entry_is_left_alone(self):
        closed_at = timezone.now()
        entry = TimeEntry.objects.create(
            membership=self.membership,
            clock_in_at=closed_at,
            clock_out_at=closed_at,
            status=TimeEntry.Status.CLOCKED_OUT,
        )
        self._deactivate(self.membership)

        entry.refresh_from_db()
        self.assertEqual(entry.clock_out_at, closed_at)
        self.assertFalse(entry.auto_closed_on_deactivation)

    def test_deactivating_an_unrelated_membership_does_not_close_this_one(self):
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        self._deactivate(self.other_membership)

        entry.refresh_from_db()
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_IN)
        self.assertFalse(entry.auto_closed_on_deactivation)

    # --- ShiftSwapRequest -----------------------------------------------------------

    def test_pending_swap_request_as_requester_is_cancelled(self):
        start = timezone.now()
        shift = EmployeeShift.objects.create(
            membership=self.membership, position=self.position, start_at=start, end_at=start + timezone.timedelta(hours=8)
        )
        swap = ShiftSwapRequest.objects.create(shift=shift, requesting_membership=self.membership)

        self._deactivate(self.membership)

        swap.refresh_from_db()
        self.assertEqual(swap.status, ShiftSwapRequest.Status.CANCELLED)

    def test_pending_swap_request_as_target_is_also_cancelled(self):
        """The membership being deactivated didn't request the swap — they were the target someone else wanted to swap with."""
        start = timezone.now()
        shift = EmployeeShift.objects.create(
            membership=self.other_membership, position=self.position, start_at=start, end_at=start + timezone.timedelta(hours=8)
        )
        swap = ShiftSwapRequest.objects.create(
            shift=shift, requesting_membership=self.other_membership, target_membership=self.membership
        )

        self._deactivate(self.membership)

        swap.refresh_from_db()
        self.assertEqual(swap.status, ShiftSwapRequest.Status.CANCELLED)

    def test_already_approved_swap_request_is_left_alone(self):
        start = timezone.now()
        shift = EmployeeShift.objects.create(
            membership=self.membership, position=self.position, start_at=start, end_at=start + timezone.timedelta(hours=8)
        )
        swap = ShiftSwapRequest.objects.create(
            shift=shift, requesting_membership=self.membership, status=ShiftSwapRequest.Status.APPROVED
        )

        self._deactivate(self.membership)

        swap.refresh_from_db()
        self.assertEqual(swap.status, ShiftSwapRequest.Status.APPROVED)

    # --- TimeOffRequest ---------------------------------------------------------------

    def test_pending_time_off_request_is_cancelled(self):
        today = timezone.now().date()
        time_off = TimeOffRequest.objects.create(membership=self.membership, start_date=today, end_date=today)

        self._deactivate(self.membership)

        time_off.refresh_from_db()
        self.assertEqual(time_off.status, TimeOffRequest.Status.CANCELLED)

    def test_already_rejected_time_off_request_is_left_alone(self):
        today = timezone.now().date()
        time_off = TimeOffRequest.objects.create(
            membership=self.membership, start_date=today, end_date=today, status=TimeOffRequest.Status.REJECTED
        )

        self._deactivate(self.membership)

        time_off.refresh_from_db()
        self.assertEqual(time_off.status, TimeOffRequest.Status.REJECTED)

    # --- Idempotency / signal-wiring specifics ---------------------------------------

    def test_running_the_cascade_twice_is_a_no_op_the_second_time(self):
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        self._deactivate(self.membership)
        entry.refresh_from_db()
        first_clock_out = entry.clock_out_at

        services.handle_membership_deactivation(self.membership)

        entry.refresh_from_db()
        self.assertEqual(entry.clock_out_at, first_clock_out)

    def test_resaving_an_already_deactivated_membership_does_not_reopen_or_touch_anything(self):
        """Confirms the pre_save/post_save transition-detection: this isn't re-run on every save of an inactive row."""
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        self._deactivate(self.membership)
        entry.refresh_from_db()
        closed_at = entry.clock_out_at

        # Re-save while still inactive — no new True->False transition.
        self.membership.save()

        entry.refresh_from_db()
        self.assertEqual(entry.clock_out_at, closed_at)

    def test_creating_a_membership_as_inactive_from_the_start_does_not_trigger_the_cascade(self):
        """No prior True state existed to transition away from — created=True is excluded explicitly."""
        entry_owner_membership = BusinessMembership.objects.create(
            business=self.business,
            user=User.objects.create_user(email="created-inactive@example.com"),
            role=BusinessMembership.Role.STAFF,
            is_active=False,
        )
        # Nothing to assert on a TimeEntry here (none could exist before
        # creation) — the real assertion is just that creation itself
        # doesn't raise and the membership ends up inactive as requested.
        self.assertFalse(entry_owner_membership.is_active)

    def test_reactivation_does_not_trigger_the_deactivation_cascade(self):
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())
        self._deactivate(self.membership)

        self.membership.is_active = True
        self.membership.save(update_fields=["is_active"])

        # The entry was already force-closed by the earlier deactivation —
        # reactivating doesn't reopen it or do anything else new.
        entry.refresh_from_db()
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_OUT)
        self.assertTrue(entry.auto_closed_on_deactivation)

    def test_cascade_fires_via_admin_style_full_save_not_just_update_fields(self):
        """Confirms the signal wiring itself, not just calling the service function directly."""
        entry = TimeEntry.objects.create(membership=self.membership, clock_in_at=timezone.now())

        membership = BusinessMembership.objects.get(pk=self.membership.pk)
        membership.is_active = False
        membership.save()  # full save, no update_fields — what a Django admin form submission does

        entry.refresh_from_db()
        self.assertEqual(entry.status, TimeEntry.Status.CLOCKED_OUT)
        self.assertTrue(entry.auto_closed_on_deactivation)
