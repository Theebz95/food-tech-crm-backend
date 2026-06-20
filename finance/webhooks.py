"""
Stripe webhook endpoint. This did not exist in the original Supabase system
— the old create-checkout Edge Function only ever created a Checkout
session; nothing synced completion, renewals, or cancellations back into
the database. Built correctly from scratch here, not ported — and now
actually implemented (it was previously wired up with TODO stubs only).

Register this URL with Stripe (or the Stripe CLI for local dev) pointing at:
    POST /api/finance/webhooks/stripe/

Stripe requires the *raw* request body for signature verification, which is
why this is a plain DRF APIView with authentication disabled (Stripe can't
send our Supabase JWT) rather than going through SupabaseAuthentication —
trust is established entirely via STRIPE_WEBHOOK_SECRET signature checking.

Idempotency: Stripe can and will redeliver the same event (network
retries, timeouts). `StripeWebhookEvent.event_id` is the primary key, so
inserting one is an atomic "have I seen this before" check with no
check-then-insert race. That insert and the handler's side effects share
one `transaction.atomic()` block: if the handler raises, the whole thing
(including the dedup row) rolls back, so a Stripe retry after a failure
is treated as genuinely new and reprocessed cleanly — there is no state
where an event is marked "seen" but its side effects only half-applied.

Important distinction the `invoice.paid` handler depends on: Stripe's own
"Invoice" object (subscription billing — the Business paying *us* for
SaaS access) is a completely different concept from this codebase's
`finance.Invoice` (the Business billing *their own* customers). This
handler only ever touches `core.Business` fields, never creates a
`finance.Payment` or touches a `finance.Invoice` — conflating the two
would be a real bug, not a naming nitpick.
"""

import logging
import uuid

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Business

from .models import StripeWebhookEvent

stripe.api_key = settings.STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)


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

        try:
            with transaction.atomic():
                try:
                    StripeWebhookEvent.objects.create(event_id=event["id"], event_type=event["type"])
                except IntegrityError:
                    # Already processed — idempotent no-op. Still 200, so
                    # Stripe doesn't keep retrying a "failure" that isn't one.
                    return Response(status=200)

                handler_name = self.EVENT_HANDLERS.get(event["type"])
                if handler_name:
                    getattr(self, handler_name)(event["data"]["object"])
        except Exception:
            # Whole transaction (dedup row + any partial side effects)
            # rolled back together — see module docstring.
            return Response(status=500)

        return Response(status=200)

    def _handle_checkout_completed(self, session):
        """
        Expects `client_reference_id` set to the Business's UUID when the
        Checkout Session was created — that creation step is the other
        half of this integration and isn't built in this session (this
        webhook only consumes what Stripe sends back). An unresolvable
        reference is a no-op (the event is still marked processed —
        retrying won't make a missing/invalid reference resolve), but it
        is always logged: a checkout session with no usable
        client_reference_id almost always means the *other* half of this
        integration (creating the session) is misconfigured, and that's
        worth knowing about, not silently dropping.
        """
        raw_reference_id = session.get("client_reference_id")
        try:
            reference_id = uuid.UUID(str(raw_reference_id))
        except (ValueError, TypeError):
            logger.warning(
                "Stripe checkout.session.completed has no usable client_reference_id "
                "(got %r) — cannot link to a Business. customer=%s subscription=%s",
                raw_reference_id,
                session.get("customer"),
                session.get("subscription"),
            )
            return
        business = Business.objects.filter(id=reference_id).first()
        if business is None:
            logger.warning(
                "Stripe checkout.session.completed client_reference_id=%s does not match any Business. "
                "customer=%s subscription=%s",
                reference_id,
                session.get("customer"),
                session.get("subscription"),
            )
            return
        business.stripe_customer_id = session.get("customer", "") or business.stripe_customer_id
        business.stripe_subscription_id = session.get("subscription", "") or business.stripe_subscription_id
        business.subscription_status = "active"
        business.is_active = True
        business.save(update_fields=["stripe_customer_id", "stripe_subscription_id", "subscription_status", "is_active", "updated_at"])

    def _handle_subscription_updated(self, subscription):
        business = Business.objects.filter(stripe_subscription_id=subscription["id"]).first()
        if business is None:
            logger.warning(
                "Stripe customer.subscription.updated for subscription=%s does not match any Business.",
                subscription.get("id"),
            )
            return
        business.subscription_status = subscription.get("status", business.subscription_status)
        business.save(update_fields=["subscription_status", "updated_at"])

    def _handle_subscription_deleted(self, subscription):
        business = Business.objects.filter(stripe_subscription_id=subscription["id"]).first()
        if business is None:
            logger.warning(
                "Stripe customer.subscription.deleted for subscription=%s does not match any Business.",
                subscription.get("id"),
            )
            return
        business.subscription_status = "canceled"
        if not business.is_legacy:
            business.is_active = False
        business.save(update_fields=["subscription_status", "is_active", "updated_at"])

    def _handle_invoice_paid(self, stripe_invoice):
        """
        Stripe's subscription-billing Invoice, not finance.Invoice — see
        module docstring. Covers a past_due subscription catching back up.
        """
        business = Business.objects.filter(stripe_customer_id=stripe_invoice.get("customer")).first()
        if business is None:
            logger.warning(
                "Stripe invoice.paid for customer=%s does not match any Business.",
                stripe_invoice.get("customer"),
            )
            return
        business.is_active = True
        if business.subscription_status == "past_due":
            business.subscription_status = "active"
        business.save(update_fields=["is_active", "subscription_status", "updated_at"])
