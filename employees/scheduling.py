"""
Recurring schedule expansion: turns a RecurringSchedule rule into real,
persisted EmployeeShift rows.

This is the direct fix for the Phase 1 audit finding that recurring
schedules were expanded client-side, on every render, with nothing ever
persisted — there was no durable record of "this employee works Tuesday"
until a browser happened to compute it. `expand_recurring_schedule` (called
by the Celery task in employees/tasks.py) is the only thing that creates
EmployeeShift rows from a RecurringSchedule, and it's idempotent: calling
it again for a date that already has a shift is a no-op, backed by the
`unique_shift_per_recurring_schedule_occurrence` DB constraint
(EmployeeShift.Meta.constraints) as well as the get_or_create() below.

The actual weekly/biweekly/monthly date-stepping logic lives in
`core.recurrence` — extracted there so finance.recurring (RecurringTransaction
-> Invoice/Bill expansion) can reuse it instead of duplicating it. `occurrence_dates`
below is a thin wrapper preserving this module's original signature/behavior.
"""

from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from core.recurrence import occurrence_dates as _shared_occurrence_dates

from .models import EmployeeShift, RecurringSchedule


def occurrence_dates(schedule: RecurringSchedule, window_start, window_end):
    """
    Yields every date in [window_start, window_end] (inclusive) on which
    `schedule` should produce a shift, given its recurrence_rule and the
    schedule's own start_date/end_date bounds.
    """
    yield from _shared_occurrence_dates(
        schedule.recurrence_rule,
        schedule.start_date,
        schedule.end_date,
        schedule.shift_template.day_of_week,
        window_start,
        window_end,
    )


def expand_recurring_schedule(schedule: RecurringSchedule, window_start, window_end) -> int:
    """Creates any missing EmployeeShift rows for `schedule` in the window. Returns count created."""
    template = schedule.shift_template
    crosses_midnight = template.end_time <= template.start_time
    created_count = 0

    for occurrence_date in occurrence_dates(schedule, window_start, window_end):
        start_at = timezone.make_aware(datetime.combine(occurrence_date, template.start_time))
        end_date = occurrence_date + timedelta(days=1) if crosses_midnight else occurrence_date
        end_at = timezone.make_aware(datetime.combine(end_date, template.end_time))

        _, was_created = EmployeeShift.objects.get_or_create(
            recurring_schedule=schedule,
            start_at=start_at,
            defaults={
                "membership": schedule.membership,
                "position": template.position,
                "end_at": end_at,
                "status": EmployeeShift.Status.SCHEDULED,
            },
        )
        if was_created:
            created_count += 1

    return created_count


def expand_active_recurring_schedules(window_days=28) -> int:
    """
    Rolling expansion: ensures every active RecurringSchedule has shifts
    generated through `window_days` from today. Safe to call repeatedly —
    see module docstring.
    """
    today = timezone.now().date()
    window_end = today + timedelta(days=window_days)

    active_schedules = RecurringSchedule.objects.filter(
        is_active=True, start_date__lte=window_end
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).select_related("shift_template")

    total_created = 0
    for schedule in active_schedules:
        total_created += expand_recurring_schedule(schedule, today, window_end)
    return total_created
