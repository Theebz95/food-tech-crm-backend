from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from authentication.models import User
from core.models import Business, BusinessMembership

from . import payroll
from .models import Position, TimeEntry, TimeEntryBreak


def aware(d, hour, minute=0):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour, minute))


class PayStubCalculationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner-pay@example.com")
        self.business = Business.objects.create(name="Pay Co", owner=owner)
        user = User.objects.create_user(email="staff-pay@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))
        # A Monday, so a single TimeEntry within the same week stays in one ISO week.
        self.monday = date(2026, 1, 5)

    def test_regular_and_overtime_split_hand_calculated(self):
        # 45 hours in one week: 40 regular @ $20 + 5 overtime @ $30 (1.5x)
        # = 800 + 150 = 950 gross. 15% placeholder tax = 142.50. Net = 807.50.
        TimeEntry.objects.create(
            membership=self.membership,
            clock_in_at=aware(self.monday, 0),
            clock_out_at=aware(self.monday, 0) + timedelta(hours=45),
            status=TimeEntry.Status.CLOCKED_OUT,
        )

        pay_stub = payroll.generate_pay_stub(self.membership, self.position, self.monday, self.monday + timedelta(days=6))

        self.assertEqual(pay_stub.regular_hours, Decimal("40.00"))
        self.assertEqual(pay_stub.overtime_hours, Decimal("5.00"))
        self.assertEqual(pay_stub.gross_pay, Decimal("950.00"))
        self.assertEqual(pay_stub.net_pay, Decimal("807.50"))
        self.assertEqual(pay_stub.breakdown["tax_amount"], "142.50")
        self.assertIn("PLACEHOLDER", pay_stub.breakdown["tax_disclaimer"])
        self.assertEqual(len(pay_stub.breakdown["weekly_breakdown"]), 1)

    def test_break_time_is_subtracted_from_worked_hours(self):
        # 9am-5pm (8h) minus a 1h break = 7h, all regular.
        # Gross = 7 * 20 = 140.00. Tax = 21.00. Net = 119.00.
        entry = TimeEntry.objects.create(
            membership=self.membership,
            clock_in_at=aware(self.monday, 9),
            clock_out_at=aware(self.monday, 17),
            status=TimeEntry.Status.CLOCKED_OUT,
        )
        TimeEntryBreak.objects.create(
            time_entry=entry, break_start_at=aware(self.monday, 12), break_end_at=aware(self.monday, 13)
        )

        pay_stub = payroll.generate_pay_stub(self.membership, self.position, self.monday, self.monday + timedelta(days=6))

        self.assertEqual(pay_stub.regular_hours, Decimal("7.00"))
        self.assertEqual(pay_stub.overtime_hours, Decimal("0.00"))
        self.assertEqual(pay_stub.gross_pay, Decimal("140.00"))
        self.assertEqual(pay_stub.net_pay, Decimal("119.00"))

    def test_open_time_entry_is_not_counted(self):
        TimeEntry.objects.create(
            membership=self.membership, clock_in_at=aware(self.monday, 9), status=TimeEntry.Status.CLOCKED_IN
        )
        pay_stub = payroll.generate_pay_stub(self.membership, self.position, self.monday, self.monday + timedelta(days=6))
        self.assertEqual(pay_stub.regular_hours, Decimal("0.00"))
        self.assertEqual(pay_stub.gross_pay, Decimal("0.00"))

    def test_duplicate_pay_stub_for_same_period_rejected(self):
        period_end = self.monday + timedelta(days=6)
        payroll.generate_pay_stub(self.membership, self.position, self.monday, period_end)
        with self.assertRaises(payroll.PayStubAlreadyExistsError):
            payroll.generate_pay_stub(self.membership, self.position, self.monday, period_end)

    def test_overtime_threshold_is_business_configurable(self):
        self.business.extra_settings = {"overtime_threshold_hours": 30}
        self.business.save()
        # 35 hours in one week against a 30h threshold: 30 regular + 5 overtime.
        # Gross = 30*20 + 5*30 = 600 + 150 = 750. Tax = 112.50. Net = 637.50.
        TimeEntry.objects.create(
            membership=self.membership,
            clock_in_at=aware(self.monday, 0),
            clock_out_at=aware(self.monday, 0) + timedelta(hours=35),
            status=TimeEntry.Status.CLOCKED_OUT,
        )

        pay_stub = payroll.generate_pay_stub(self.membership, self.position, self.monday, self.monday + timedelta(days=6))

        self.assertEqual(pay_stub.regular_hours, Decimal("30.00"))
        self.assertEqual(pay_stub.overtime_hours, Decimal("5.00"))
        self.assertEqual(pay_stub.gross_pay, Decimal("750.00"))
        self.assertEqual(pay_stub.net_pay, Decimal("637.50"))
