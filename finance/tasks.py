from celery import shared_task
from django.utils import timezone

from .models import Bill, Invoice
from .recurring import expand_active_recurring_transactions


@shared_task
def mark_overdue_invoices():
    """
    Daily Celery Beat task (config/settings.py CELERY_BEAT_SCHEDULE) —
    `overdue` is never computed live on read, only set here. Only `sent`
    invoices past their due_date are touched; `draft` (not yet sent),
    `paid`, `cancelled`, and already-`overdue` invoices are left alone.
    Replaces the old client-side "is this overdue" computation done on
    every page render.
    """
    Invoice.objects.filter(status=Invoice.Status.SENT, due_date__lt=timezone.now().date()).update(
        status=Invoice.Status.OVERDUE, updated_at=timezone.now()
    )


@shared_task
def mark_overdue_bills():
    """Same as mark_overdue_invoices, for Bill — only `received` bills past due_date are touched."""
    Bill.objects.filter(status=Bill.Status.RECEIVED, due_date__lt=timezone.now().date()).update(
        status=Bill.Status.OVERDUE, updated_at=timezone.now()
    )


@shared_task
def expand_recurring_transactions():
    """
    Keeps every active RecurringTransaction expanded ~4 weeks ahead into
    real Invoice/Bill rows. Replaces the old client-side, manually
    triggered generation (useGenerateRecurringTransaction() in the old
    frontend, src/hooks/useRecurringTransactions.ts) — there was no real
    scheduler at all before this.

    This task (and the model it expands) supersedes the
    `generate_due_recurring_transactions` stub that used to be scheduled
    here: that stub's planned design (a `next_date`/`last_generated_date`
    watermark per RecurringTransaction, advanced on each generation) is
    superseded by the rolling-window + idempotent-insert pattern already
    proven for `employees.RecurringSchedule` -> `EmployeeShift` — see
    finance/recurring.py and core/recurrence.py. Runs daily via Celery
    Beat (config/settings.py -> CELERY_BEAT_SCHEDULE); safe to run twice,
    a missed run, or an overlapping run without duplicating generated
    documents.
    """
    return expand_active_recurring_transactions()
