from celery import shared_task


@shared_task
def generate_due_recurring_transactions():
    """
    Stub. Will replicate useGenerateRecurringTransaction() from the old
    frontend (src/hooks/useRecurringTransactions.ts), but server-side and
    atomic: for every RecurringTransaction with next_date <= today and
    auto_create=True, create the concrete Transaction row and advance
    next_date/last_generated_date inside a single transaction.atomic()
    block (the original did this as two separate, non-transactional client
    calls).

    Not implemented yet — RecurringTransaction doesn't exist until the
    Finance domain models are built in a follow-up session. Already wired
    into CELERY_BEAT_SCHEDULE (config/settings.py) so the schedule exists
    ahead of the implementation.
    """
    raise NotImplementedError("Finance domain models not yet built.")
