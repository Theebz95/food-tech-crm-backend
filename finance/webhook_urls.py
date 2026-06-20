"""
Just the Stripe webhook — kept separate from urls.py (the staff-side
HasBusinessRole routes) so it can stay mounted at the exact
`/api/finance/webhooks/stripe/` path already documented in webhooks.py,
while the staff CRUD routes follow the same `/api/businesses/<business_id>/...`
convention as every other domain (mounted at `api/` in config/urls.py).
"""

from django.urls import path

from .webhooks import StripeWebhookView

app_name = "finance_webhooks"

urlpatterns = [
    path("webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
]
