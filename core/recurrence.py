"""
Shared weekly/biweekly/monthly recurrence date-stepping.

Originally written for employees.scheduling (RecurringSchedule ->
EmployeeShift expansion) and extracted here so finance.recurring
(RecurringTransaction -> Invoice/Bill expansion) can reuse the exact same,
already-tested date math rather than a second independently-maintained
copy of it — per the explicit instruction to reuse rather than
reimplement when the Finance domain's recurring transactions were built.

`employees/scheduling.py:occurrence_dates` is now a thin wrapper around
`occurrence_dates` here, passing `schedule.shift_template.day_of_week` as
`weekday_target`; finance.recurring passes the recurring transaction's own
`start_date.weekday()` instead, since it has no separate template to
anchor to. Behavior for employees is unchanged — see
employees/test_scheduling.py, which still exercises this logic through
the unchanged public function signature.
"""

from datetime import timedelta


def next_weekday_on_or_after(start_date, weekday):
    """First date >= start_date falling on the given weekday (0=Monday)."""
    delta = (weekday - start_date.weekday()) % 7
    return start_date + timedelta(days=delta)


def add_month(d):
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def occurrence_dates(recurrence_rule, anchor_start_date, rule_end_date, weekday_target, window_start, window_end):
    """
    Yields every date in [window_start, window_end] (inclusive) on which a
    weekly/biweekly/monthly rule falls, given:

      - `recurrence_rule`: "weekly" / "biweekly" / "monthly" (any string-valued
        TextChoices with these values works — comparison is by value).
      - `anchor_start_date` / `rule_end_date`: the rule's own bounds (`rule_end_date`
        may be None for "ongoing").
      - `weekday_target`: which weekday (0=Monday) weekly/biweekly occurrences
        fall on. Ignored for monthly, which instead anchors to
        `anchor_start_date`'s day-of-month.
    """
    effective_start = max(anchor_start_date, window_start)
    effective_end = window_end if rule_end_date is None else min(rule_end_date, window_end)
    if effective_start > effective_end:
        return

    if recurrence_rule == "monthly":
        day_of_month = anchor_start_date.day
        current = effective_start.replace(day=1)
        while current <= effective_end:
            try:
                occurrence = current.replace(day=day_of_month)
            except ValueError:
                # e.g. day 31 in a 30-day month — skip that month rather
                # than silently shifting to a different day.
                occurrence = None
            if occurrence is not None and effective_start <= occurrence <= effective_end:
                yield occurrence
            current = add_month(current)
        return

    step_days = 7 if recurrence_rule == "weekly" else 14
    # Anchor to the rule's actual start, not the window, so a biweekly
    # rule keeps the same weekly phase every time this is called —
    # otherwise re-running against a later window could shift which weeks
    # are "on" vs "off".
    anchor = next_weekday_on_or_after(anchor_start_date, weekday_target)
    if effective_start <= anchor:
        current = anchor
    else:
        days_since_anchor = (effective_start - anchor).days
        remainder = days_since_anchor % step_days
        current = effective_start if remainder == 0 else effective_start + timedelta(days=step_days - remainder)

    while current <= effective_end:
        yield current
        current += timedelta(days=step_days)
