from celery import shared_task

from .scheduling import expand_active_recurring_schedules


@shared_task
def expand_recurring_schedules():
    """
    Keeps every active RecurringSchedule expanded ~4 weeks ahead into real
    EmployeeShift rows. Replaces the old client-side, render-time expansion
    (see employees/models.py module docstring, "Security fix #2"). Runs
    daily via Celery Beat (config/settings.py -> CELERY_BEAT_SCHEDULE);
    idempotent, so a missed run or an overlapping run never duplicates
    shifts.
    """
    return expand_active_recurring_schedules()
