from django.urls import path

from .webhooks import StripeWebhookView

app_name = "finance"

urlpatterns = [
    path("webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
]
