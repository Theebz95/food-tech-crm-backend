from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from authentication.models import User
from core.models import Business, BusinessMembership

from . import scheduling
from .models import EmployeeShift, Position, RecurringSchedule, ShiftTemplate


class OccurrenceDatesTests(TestCase):
    """
    Pure date-math tests for scheduling.occurrence_dates — no DB writes of
    EmployeeShift, just verifying which dates a recurrence rule produces
    for a given window. Deliberately avoids hardcoding which weekday a
    given calendar date falls on; the template's day_of_week is always
    derived from the chosen start date instead.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner-sched@example.com")
        self.business = Business.objects.create(name="Schedule Co", owner=owner)
        user = User.objects.create_user(email="staff-sched@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))

    def make_schedule(self, start_date, recurrence_rule, end_date=None):
        template = ShiftTemplate.objects.create(
            business=self.business,
            position=self.position,
            day_of_week=start_date.weekday(),
            start_time="09:00",
            end_time="17:00",
        )
        return RecurringSchedule.objects.create(
            membership=self.membership,
            shift_template=template,
            recurrence_rule=recurrence_rule,
            start_date=start_date,
            end_date=end_date,
        )

    def test_weekly_occurrences_within_window(self):
        start = date(2026, 1, 5)
        schedule = self.make_schedule(start, RecurringSchedule.Recurrence.WEEKLY)
        occurrences = list(scheduling.occurrence_dates(schedule, start, start + timedelta(days=27)))
        self.assertEqual(occurrences, [start, start + timedelta(days=7), start + timedelta(days=14), start + timedelta(days=21)])

    def test_biweekly_occurrences_within_window(self):
        start = date(2026, 1, 5)
        schedule = self.make_schedule(start, RecurringSchedule.Recurrence.BIWEEKLY)
        occurrences = list(scheduling.occurrence_dates(schedule, start, start + timedelta(days=27)))
        self.assertEqual(occurrences, [start, start + timedelta(days=14)])

    def test_biweekly_keeps_phase_when_window_starts_mid_cycle(self):
        """
        The whole point of anchoring to schedule.start_date rather than the
        window: if the window starts a week after the schedule's own
        cadence, the off-phase week must NOT produce a shift.
        """
        start = date(2026, 1, 5)
        schedule = self.make_schedule(start, RecurringSchedule.Recurrence.BIWEEKLY)
        window_start = start + timedelta(days=7)  # one week into the cycle — off-phase
        window_end = start + timedelta(days=21)
        occurrences = list(scheduling.occurrence_dates(schedule, window_start, window_end))
        self.assertEqual(occurrences, [start + timedelta(days=14)])

    def test_monthly_occurrences_within_window(self):
        start = date(2026, 1, 15)
        schedule = self.make_schedule(start, RecurringSchedule.Recurrence.MONTHLY)
        occurrences = list(scheduling.occurrence_dates(schedule, start, start + timedelta(days=89)))
        self.assertEqual(occurrences, [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)])

    def test_respects_schedule_end_date(self):
        start = date(2026, 1, 5)
        schedule = self.make_schedule(start, RecurringSchedule.Recurrence.WEEKLY, end_date=start + timedelta(days=10))
        occurrences = list(scheduling.occurrence_dates(schedule, start, start + timedelta(days=27)))
        self.assertEqual(occurrences, [start, start + timedelta(days=7)])


class ExpandRecurringSchedulesTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner-expand@example.com")
        self.business = Business.objects.create(name="Expand Co", owner=owner)
        user = User.objects.create_user(email="staff-expand@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))
        self.today = timezone.now().date()
        self.template = ShiftTemplate.objects.create(
            business=self.business,
            position=self.position,
            day_of_week=self.today.weekday(),
            start_time="09:00",
            end_time="17:00",
        )
        self.schedule = RecurringSchedule.objects.create(
            membership=self.membership,
            shift_template=self.template,
            recurrence_rule=RecurringSchedule.Recurrence.WEEKLY,
            start_date=self.today - timedelta(days=60),
        )

    def test_expand_creates_expected_shifts(self):
        window_end = self.today + timedelta(days=28)
        expected_dates = list(scheduling.occurrence_dates(self.schedule, self.today, window_end))

        created_count = scheduling.expand_active_recurring_schedules(window_days=28)

        self.assertEqual(created_count, len(expected_dates))
        shifts = EmployeeShift.objects.filter(recurring_schedule=self.schedule)
        self.assertEqual(sorted(s.start_at.date() for s in shifts), sorted(expected_dates))
        for shift in shifts:
            self.assertEqual(shift.membership_id, self.membership.id)
            self.assertEqual(shift.position_id, self.position.id)
            self.assertEqual(shift.status, EmployeeShift.Status.SCHEDULED)

    def test_expand_is_idempotent(self):
        first_run_count = scheduling.expand_active_recurring_schedules(window_days=28)
        second_run_count = scheduling.expand_active_recurring_schedules(window_days=28)

        self.assertGreater(first_run_count, 0)
        self.assertEqual(second_run_count, 0)
        self.assertEqual(EmployeeShift.objects.filter(recurring_schedule=self.schedule).count(), first_run_count)

    def test_paused_schedule_is_not_expanded(self):
        self.schedule.is_active = False
        self.schedule.save()
        created_count = scheduling.expand_active_recurring_schedules(window_days=28)
        self.assertEqual(created_count, 0)

    def test_overnight_shift_template_ends_next_day(self):
        self.template.start_time = "22:00"
        self.template.end_time = "06:00"
        self.template.save()

        scheduling.expand_active_recurring_schedules(window_days=28)

        shift = EmployeeShift.objects.filter(recurring_schedule=self.schedule).earliest("start_at")
        self.assertEqual(shift.end_at.date(), shift.start_at.date() + timedelta(days=1))
