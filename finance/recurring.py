"""
Recurring transaction expansion: turns a RecurringTransaction rule into
real, persisted Invoice or Bill rows.

This is the direct fix for the Phase 1 audit finding that recurring
transaction generation was manually triggered from the frontend, with no
real scheduler. Mirrors employees.scheduling's RecurringSchedule ->
EmployeeShift expansion exactly — including reusing its actual
date-stepping logic (core.recurrence.occurrence_dates) rather than a
second implementation of the same weekly/biweekly/monthly math.

Idempotency here works differently from EmployeeShift's plain
get_or_create(), because creating an Invoice/Bill is a multi-step
operation (sequence number, line items, tax calculation) that doesn't fit
get_or_create()'s "defaults dict on a single insert" shape. Instead: a
cheap pre-check (has a row for this occurrence already been created?)
skips the common case cheaply, and the actual creation is wrapped in
transaction.atomic() with the DB's own unique constraint
(unique_invoice_per_recurring_transaction_occurrence /
unique_bill_per_recurring_transaction_occurrence) as the real safety net
— a concurrent run that loses the race gets IntegrityError, which is
caught and treated as "already handled," not an error. Same shape as
StripeWebhookEvent's insert-first idempotency in webhooks.py.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.recurrence import occurrence_dates as _occurrence_dates

from . import services
from .models import Bill, Invoice, RecurringTransaction


def expand_recurring_transaction(recurring_transaction: RecurringTransaction, window_start, window_end) -> int:
    """Creates any missing Invoice/Bill rows for `recurring_transaction` in the window. Returns count created."""
    is_invoice = recurring_transaction.kind == RecurringTransaction.Kind.INVOICE
    model = Invoice if is_invoice else Bill
    weekday_target = recurring_transaction.start_date.weekday()
    created_count = 0

    for occurrence_date in _occurrence_dates(
        recurring_transaction.recurrence_rule,
        recurring_transaction.start_date,
        recurring_transaction.end_date,
        weekday_target,
        window_start,
        window_end,
    ):
        already_exists = model.objects.filter(
            recurring_transaction=recurring_transaction, recurring_occurrence_date=occurrence_date
        ).exists()
        if already_exists:
            continue

        due_date = occurrence_date + timedelta(days=recurring_transaction.due_in_days)
        try:
            with transaction.atomic():
                if is_invoice:
                    services.create_invoice(
                        business=recurring_transaction.business,
                        customer=recurring_transaction.customer,
                        line_items_data=recurring_transaction.line_item_presets,
                        tax_type=recurring_transaction.tax_type,
                        discount_type=recurring_transaction.discount_type,
                        discount_value=recurring_transaction.discount_value,
                        due_date=due_date,
                        notes=recurring_transaction.notes,
                        recurring_transaction=recurring_transaction,
                        recurring_occurrence_date=occurrence_date,
                    )
                else:
                    services.create_bill(
                        business=recurring_transaction.business,
                        vendor=recurring_transaction.vendor,
                        line_items_data=recurring_transaction.line_item_presets,
                        tax_type=recurring_transaction.tax_type,
                        discount_type=recurring_transaction.discount_type,
                        discount_value=recurring_transaction.discount_value,
                        due_date=due_date,
                        notes=recurring_transaction.notes,
                        recurring_transaction=recurring_transaction,
                        recurring_occurrence_date=occurrence_date,
                    )
        except IntegrityError:
            # A concurrent run already created this occurrence between
            # our pre-check above and our insert — the unique constraint
            # caught it; nothing more to do for this occurrence.
            continue
        created_count += 1

    return created_count


def expand_active_recurring_transactions(window_days=28) -> int:
    """
    Rolling expansion: ensures every active RecurringTransaction has
    Invoice/Bill rows generated through `window_days` from today. Safe to
    call repeatedly — see module docstring.
    """
    today = timezone.now().date()
    window_end = today + timedelta(days=window_days)

    active = RecurringTransaction.objects.filter(is_active=True, start_date__lte=window_end).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    )

    total_created = 0
    for recurring_transaction in active:
        total_created += expand_recurring_transaction(recurring_transaction, today, window_end)
    return total_created
