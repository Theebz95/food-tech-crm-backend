"""
Finance domain (invoices, payments, bills, transactions, recurring
transactions, chart of accounts) — models not yet built. Deferred to a
follow-up session once the core tenancy app is confirmed working
end-to-end.

The Stripe webhook endpoint (webhooks.py) is implemented now even though
the rest of this app is empty, because it's new infrastructure that did not
exist in the original system (the old create-checkout Edge Function only
ever started a Stripe Checkout session; nothing ever synced completion back
to the database). It reads/writes `Business.stripe_customer_id` /
`stripe_subscription_id` / `subscription_status` on the core app's Business
model — those fields exist specifically to support this webhook.

The recurring-transaction generation Celery task (tasks.py) is stubbed for
the same reason: it's wired into Celery Beat now so the schedule exists,
but it raises NotImplementedError until RecurringTransaction is built.

When this domain is built, every status transition that the old frontend
computed client-side (invoice draft -> sent -> paid -> overdue, bill
unpaid -> partial -> paid) should move into service-layer functions wrapped
in transaction.atomic(), not into model save() side effects, since they
often involve more than one row (e.g. creating a Payment also has to update
the related Invoice's status).
"""
