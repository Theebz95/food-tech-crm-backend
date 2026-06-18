"""
Stripe webhook endpoint. This did not exist in the original Supabase system
— the old create-checkout Edge Function only ever created a Checkout
session; nothing synced completion, renewals, or cancellations back into
the database. Built correctly from scratch here, not ported.

Register this URL with Stripe (or the Stripe CLI for local dev) pointing at:
    POST /api/finance/webhooks/stripe/

Stripe requires the *raw* request body for signature verification, which is
why this is a plain DRF APIView with authentication disabled (Stripe can't
send our Supabase JWT) rather than going through SupabaseAuthentication —
trust is established entirely via STRIPE_WEBHOOK_SECRET signature checking.
"""

import stripe
from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

stripe.api_key = settings.STRIPE_SECRET_KEY


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    EVENT_HANDLERS = {
        "checkout.session.completed": "_handle_checkout_completed",
        "customer.subscription.updated": "_handle_subscription_updated",
        "customer.subscription.deleted": "_handle_subscription_deleted",
        "invoice.paid": "_handle_invoice_paid",
    }

    def post(self, request, *args, **kwargs):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
        except ValueError:
            return Response(status=400)  # malformed payload
        except stripe.error.SignatureVerificationError:
            return Response(status=400)  # signature didn't match

        handler_name = self.EVENT_HANDLERS.get(event["type"])
        if handler_name:
            getattr(self, handler_name)(event["data"]["object"])

        return Response(status=200)

    def _handle_checkout_completed(self, session):
        # TODO once the Finance/Business subscription fields are wired up:
        #   - look up Business via session["client_reference_id"]
        #     (set this when creating the Checkout session) or by matching
        #     customer email as a fallback.
        #   - set business.stripe_customer_id = session["customer"]
        #   - set business.stripe_subscription_id = session["subscription"]
        #   - set business.subscription_status = "trialing" or "active"
        #   - set business.is_active = True
        pass

    def _handle_subscription_updated(self, subscription):
        # TODO: find Business by stripe_subscription_id == subscription["id"],
        # sync business.subscription_status = subscription["status"].
        pass

    def _handle_subscription_deleted(self, subscription):
        # TODO: find Business by stripe_subscription_id, set
        # subscription_status = "canceled", and is_active = False unless
        # the business is_legacy.
        pass

    def _handle_invoice_paid(self, invoice):
        # TODO: record the successful payment and ensure
        # business.is_active = True (covers the case where a past_due
        # subscription catches up on payment).
        pass
