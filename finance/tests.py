"""
Tests for the Finance domain (part 1): tax calculation against
hand-computed cases, payment concurrency/overpayment, Stripe webhook
signature verification + idempotency, the daily overdue task, and tenant
isolation (same rigor as prior domains).
"""

import hashlib
import hmac
import json
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership
from customers.models import Customer

from . import services
from .models import ChartOfAccount, Estimate, Invoice, Payment, StripeWebhookEvent
from .tasks import mark_overdue_invoices
from .tax import DiscountType, TaxLineItem, TaxType, calculate_totals, get_tax_rate

STRIPE_WEBHOOK_URL = "/api/finance/webhooks/stripe/"


def invoice_list_url(business_id):
    return f"/api/businesses/{business_id}/invoices/"


def invoice_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/invoices/{pk}/"


def invoice_record_payment_url(business_id, pk):
    return f"/api/businesses/{business_id}/invoices/{pk}/record-payment/"


def estimate_list_url(business_id):
    return f"/api/businesses/{business_id}/estimates/"


def account_list_url(business_id):
    return f"/api/businesses/{business_id}/accounts/"


def payment_list_url(business_id):
    return f"/api/businesses/{business_id}/payments/"


def _stripe_signature_header(payload: bytes, secret: str, timestamp=None) -> str:
    """Builds a real Stripe webhook signature (documented public format) — no network call needed."""
    ts = timestamp or int(time.time())
    signed_payload = f"{ts}.{payload.decode()}"
    signature = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


class TaxCalculationTests(TestCase):
    """Hand-computed expected values — see finance/tax.py for the ported source."""

    def test_no_tax_no_discount(self):
        result = calculate_totals([TaxLineItem(Decimal("2"), Decimal("50"))], DiscountType.NONE, Decimal("0"), TaxType.ZERO)
        self.assertEqual(result.subtotal, Decimal("100.00"))
        self.assertEqual(result.discount, Decimal("0.00"))
        self.assertEqual(result.taxable_amount, Decimal("100.00"))
        self.assertEqual(result.tax, Decimal("0.00"))
        self.assertEqual(result.total, Decimal("100.00"))

    def test_gst_5(self):
        result = calculate_totals([TaxLineItem(Decimal("1"), Decimal("1000"))], DiscountType.NONE, Decimal("0"), TaxType.GST_5)
        self.assertEqual(result.subtotal, Decimal("1000.00"))
        self.assertEqual(result.tax, Decimal("50.00"))
        self.assertEqual(result.total, Decimal("1050.00"))

    def test_hst_15_with_percentage_discount(self):
        result = calculate_totals(
            [TaxLineItem(Decimal("3"), Decimal("100"))], DiscountType.PERCENTAGE, Decimal("10"), TaxType.HST_15
        )
        self.assertEqual(result.subtotal, Decimal("300.00"))
        self.assertEqual(result.discount, Decimal("30.00"))
        self.assertEqual(result.taxable_amount, Decimal("270.00"))
        self.assertEqual(result.tax, Decimal("40.50"))
        self.assertEqual(result.total, Decimal("310.50"))

    def test_fixed_discount_larger_than_subtotal_floors_at_zero(self):
        result = calculate_totals(
            [TaxLineItem(Decimal("1"), Decimal("500"))], DiscountType.FIXED, Decimal("600"), TaxType.HST_15
        )
        self.assertEqual(result.discount, Decimal("600.00"))
        self.assertEqual(result.taxable_amount, Decimal("0.00"))
        self.assertEqual(result.tax, Decimal("0.00"))
        self.assertEqual(result.total, Decimal("0.00"))

    def test_gst_qst_14975(self):
        result = calculate_totals(
            [TaxLineItem(Decimal("1"), Decimal("1000"))], DiscountType.NONE, Decimal("0"), TaxType.GST_QST_14975
        )
        self.assertEqual(result.tax, Decimal("149.75"))
        self.assertEqual(result.total, Decimal("1149.75"))

    def test_qst_9975(self):
        result = calculate_totals(
            [TaxLineItem(Decimal("1"), Decimal("1000"))], DiscountType.NONE, Decimal("0"), TaxType.QST_9975
        )
        self.assertEqual(result.tax, Decimal("99.75"))
        self.assertEqual(result.total, Decimal("1099.75"))

    def test_unrecognized_tax_type_falls_back_to_zero_not_an_error(self):
        # Mirrors the original's `|| 0` fallback exactly — see get_tax_rate's docstring.
        self.assertEqual(get_tax_rate("NOT_A_REAL_TYPE"), Decimal("0"))

    def test_multiple_line_items_summed_correctly(self):
        items = [TaxLineItem(Decimal("2"), Decimal("25")), TaxLineItem(Decimal("1"), Decimal("50"))]
        result = calculate_totals(items, DiscountType.NONE, Decimal("0"), TaxType.GST_5)
        self.assertEqual(result.subtotal, Decimal("100.00"))
        self.assertEqual(result.tax, Decimal("5.00"))


class InvoiceCreationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_inv@example.com")
        self.business = Business.objects.create(name="Invoice Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Alice")
        self.staff_user = User.objects.create_user(email="staff_inv@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_invoice_computes_totals_via_the_tax_service(self):
        response = self.client.post(
            invoice_list_url(self.business.id),
            {
                "customer": str(self.customer.id),
                "tax_type": "GST_5",
                "line_items": [{"description": "Widget", "quantity": "2", "unit_price": "50.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["subtotal"], "100.00")
        self.assertEqual(response.data["tax_amount"], "5.00")
        self.assertEqual(response.data["total"], "105.00")
        self.assertEqual(response.data["status"], "draft")
        self.assertTrue(response.data["invoice_number"].startswith("INV-"))

    def test_invoice_numbers_are_sequential_per_business(self):
        first = services.create_invoice(
            self.business, self.customer, [{"description": "A", "quantity": Decimal("1"), "unit_price": Decimal("10")}], TaxType.ZERO
        )
        second = services.create_invoice(
            self.business, self.customer, [{"description": "B", "quantity": Decimal("1"), "unit_price": Decimal("10")}], TaxType.ZERO
        )
        self.assertEqual(first.invoice_number, "INV-00001")
        self.assertEqual(second.invoice_number, "INV-00002")

    def test_requires_at_least_one_line_item(self):
        response = self.client.post(
            invoice_list_url(self.business.id),
            {"customer": str(self.customer.id), "tax_type": "ZERO", "line_items": []},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_use_a_customer_from_another_business(self):
        other_owner = User.objects.create_user(email="other_owner_inv@example.com")
        other_business = Business.objects.create(name="Other Biz", owner=other_owner)
        other_customer = Customer.objects.create(business=other_business, name="Mallory")
        response = self.client.post(
            invoice_list_url(self.business.id),
            {
                "customer": str(other_customer.id),
                "tax_type": "ZERO",
                "line_items": [{"description": "X", "quantity": "1", "unit_price": "10"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Invoice.objects.filter(customer=other_customer).exists())


class PaymentRecordingTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_pay@example.com")
        self.business = Business.objects.create(name="Payment Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Bob")
        self.membership = BusinessMembership.objects.create(
            business=self.business,
            user=User.objects.create_user(email="staff_pay@example.com"),
            role=BusinessMembership.Role.STAFF,
        )
        self.invoice = services.create_invoice(
            self.business, self.customer, [{"description": "Service", "quantity": Decimal("1"), "unit_price": Decimal("100")}], TaxType.ZERO
        )
        services.send_invoice(self.invoice)

    def test_full_payment_marks_invoice_paid(self):
        services.record_payment(self.invoice, Decimal("100"), Payment.Method.CASH, self.membership)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(self.invoice.paid_total, Decimal("100"))

    def test_partial_payment_does_not_mark_paid(self):
        services.record_payment(self.invoice, Decimal("40"), Payment.Method.CASH, self.membership)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.SENT)
        self.assertEqual(self.invoice.paid_total, Decimal("40"))

    def test_two_partial_payments_summing_to_total_marks_paid(self):
        services.record_payment(self.invoice, Decimal("40"), Payment.Method.CASH, self.membership)
        services.record_payment(self.invoice, Decimal("60"), Payment.Method.CASH, self.membership)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_overpayment_is_rejected_not_silently_accepted(self):
        with self.assertRaises(services.OverpaymentError):
            services.record_payment(self.invoice, Decimal("150"), Payment.Method.CASH, self.membership)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.SENT)
        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 0)

    def test_overpayment_via_multiple_payments_is_rejected(self):
        services.record_payment(self.invoice, Decimal("80"), Payment.Method.CASH, self.membership)
        with self.assertRaises(services.OverpaymentError):
            services.record_payment(self.invoice, Decimal("30"), Payment.Method.CASH, self.membership)
        self.assertEqual(self.invoice.paid_total, Decimal("80"))

    def test_cannot_pay_a_draft_invoice(self):
        draft_invoice = services.create_invoice(
            self.business, self.customer, [{"description": "X", "quantity": Decimal("1"), "unit_price": Decimal("10")}], TaxType.ZERO
        )
        with self.assertRaises(services.InvalidInvoiceStateError):
            services.record_payment(draft_invoice, Decimal("10"), Payment.Method.CASH, self.membership)

    def test_cannot_pay_a_cancelled_invoice(self):
        services.cancel_invoice(self.invoice)
        with self.assertRaises(services.InvalidInvoiceStateError):
            services.record_payment(self.invoice, Decimal("10"), Payment.Method.CASH, self.membership)

    def test_payment_rows_are_append_only(self):
        payment = services.record_payment(self.invoice, Decimal("100"), Payment.Method.CASH, self.membership)
        payment.notes = "tampered"
        with self.assertRaises(TypeError):
            payment.save()
        with self.assertRaises(TypeError):
            payment.delete()


class PaymentConcurrencyTests(TransactionTestCase):
    """
    Invoice total=100; two concurrent payments of 80 each (sum=160>100).
    Proves select_for_update() prevents a lost update where both threads
    read "already_paid=0" and both succeed — exactly one must succeed,
    the other must be rejected for overpayment.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_concurrency_fin@example.com")
        self.business = Business.objects.create(name="Concurrency Finance Co", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Concurrent Customer")
        user = User.objects.create_user(email="staff_concurrency_fin@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.invoice = services.create_invoice(
            self.business, self.customer, [{"description": "Big job", "quantity": Decimal("1"), "unit_price": Decimal("100")}], TaxType.ZERO
        )
        services.send_invoice(self.invoice)

    def test_only_one_concurrent_payment_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_payment():
            barrier.wait()
            try:
                services.record_payment(self.invoice, Decimal("80"), Payment.Method.CASH, self.membership)
                outcome = "success"
            except services.OverpaymentError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_payment) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_total, Decimal("80"))
        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 1)


class OverdueInvoiceTaskTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_overdue@example.com")
        self.business = Business.objects.create(name="Overdue Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Carol")
        self.membership = BusinessMembership.objects.create(
            business=self.business,
            user=User.objects.create_user(email="staff_overdue@example.com"),
            role=BusinessMembership.Role.STAFF,
        )

    def _make_invoice(self, due_date, status_after_send=None):
        invoice = services.create_invoice(
            self.business, self.customer, [{"description": "X", "quantity": Decimal("1"), "unit_price": Decimal("10")}], TaxType.ZERO, due_date=due_date
        )
        services.send_invoice(invoice)
        if status_after_send == Invoice.Status.PAID:
            services.record_payment(invoice, invoice.total, Payment.Method.CASH, self.membership)
        elif status_after_send == Invoice.Status.CANCELLED:
            services.cancel_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def test_marks_only_sent_invoices_past_due_date(self):
        today = timezone.now().date()
        past_due_sent = self._make_invoice(due_date=today - timezone.timedelta(days=5))
        future_due_sent = self._make_invoice(due_date=today + timezone.timedelta(days=5))
        past_due_paid = self._make_invoice(due_date=today - timezone.timedelta(days=5), status_after_send=Invoice.Status.PAID)
        past_due_cancelled = self._make_invoice(due_date=today - timezone.timedelta(days=5), status_after_send=Invoice.Status.CANCELLED)

        mark_overdue_invoices()

        past_due_sent.refresh_from_db()
        future_due_sent.refresh_from_db()
        past_due_paid.refresh_from_db()
        past_due_cancelled.refresh_from_db()

        self.assertEqual(past_due_sent.status, Invoice.Status.OVERDUE)
        self.assertEqual(future_due_sent.status, Invoice.Status.SENT)
        self.assertEqual(past_due_paid.status, Invoice.Status.PAID)
        self.assertEqual(past_due_cancelled.status, Invoice.Status.CANCELLED)


class StripeWebhookTests(TestCase):
    def setUp(self):
        from django.conf import settings as django_settings

        self.secret = django_settings.STRIPE_WEBHOOK_SECRET
        owner = User.objects.create_user(email="owner_stripe@example.com")
        self.business = Business.objects.create(name="Stripe Biz", owner=owner)
        self.client = APIClient()

    def _post_event(self, event_dict, signature=None):
        payload = json.dumps(event_dict).encode()
        sig = signature if signature is not None else _stripe_signature_header(payload, self.secret)
        return self.client.generic(
            "POST", STRIPE_WEBHOOK_URL, data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig
        )

    def _checkout_completed_event(self, event_id="evt_test_1"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_test_123",
                    "subscription": "sub_test_123",
                    "client_reference_id": str(self.business.id),
                }
            },
        }

    def test_valid_signature_is_accepted_and_applies_the_event(self):
        response = self._post_event(self._checkout_completed_event())
        self.assertEqual(response.status_code, 200)
        self.business.refresh_from_db()
        self.assertEqual(self.business.stripe_customer_id, "cus_test_123")
        self.assertEqual(self.business.stripe_subscription_id, "sub_test_123")
        self.assertEqual(self.business.subscription_status, "active")
        self.assertTrue(self.business.is_active)

    def test_invalid_signature_is_rejected(self):
        response = self._post_event(self._checkout_completed_event(), signature="t=123,v1=deadbeef")
        self.assertEqual(response.status_code, 400)
        self.business.refresh_from_db()
        self.assertEqual(self.business.stripe_customer_id, "")

    def test_missing_signature_is_rejected(self):
        response = self._post_event(self._checkout_completed_event(), signature="")
        self.assertEqual(response.status_code, 400)

    @patch("finance.webhooks.StripeWebhookView._handle_checkout_completed")
    def test_replayed_event_id_is_a_no_op_not_double_applied(self, mock_handler):
        event = self._checkout_completed_event(event_id="evt_replay_test")
        first_response = self._post_event(event)
        second_response = self._post_event(event)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        mock_handler.assert_called_once()
        self.assertEqual(StripeWebhookEvent.objects.filter(event_id="evt_replay_test").count(), 1)

    def test_subscription_deleted_deactivates_business(self):
        self.business.stripe_subscription_id = "sub_to_cancel"
        self.business.subscription_status = "active"
        self.business.save()
        event = {
            "id": "evt_sub_deleted",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_to_cancel"}},
        }
        response = self._post_event(event)
        self.assertEqual(response.status_code, 200)
        self.business.refresh_from_db()
        self.assertEqual(self.business.subscription_status, "canceled")
        self.assertFalse(self.business.is_active)

    def test_legacy_business_is_not_deactivated_on_subscription_deleted(self):
        self.business.stripe_subscription_id = "sub_legacy"
        self.business.is_legacy = True
        self.business.save()
        event = {
            "id": "evt_sub_deleted_legacy",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_legacy"}},
        }
        self._post_event(event)
        self.business.refresh_from_db()
        self.assertTrue(self.business.is_active)

    def test_invoice_paid_does_not_create_a_finance_payment_or_invoice(self):
        # Stripe's own subscription-billing "Invoice" is unrelated to
        # finance.Invoice — see webhooks.py module docstring.
        self.business.stripe_customer_id = "cus_billing_test"
        self.business.subscription_status = "past_due"
        self.business.save()
        event = {
            "id": "evt_invoice_paid",
            "type": "invoice.paid",
            "data": {"object": {"customer": "cus_billing_test"}},
        }
        self._post_event(event)
        self.business.refresh_from_db()
        self.assertTrue(self.business.is_active)
        self.assertEqual(self.business.subscription_status, "active")
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_malformed_reference_is_a_safe_no_op_but_is_logged(self):
        event = {
            "id": "evt_malformed_ref",
            "type": "checkout.session.completed",
            "data": {
                "object": {"customer": "cus_x", "subscription": "sub_x", "client_reference_id": "not-a-uuid-at-all"}
            },
        }
        with self.assertLogs("finance.webhooks", level="WARNING") as captured:
            response = self._post_event(event)
        self.assertEqual(response.status_code, 200)
        # Still recorded as processed — a permanently-unresolvable
        # reference shouldn't make Stripe retry forever.
        self.assertTrue(StripeWebhookEvent.objects.filter(event_id="evt_malformed_ref").exists())
        # But it's not a silent swallow — this is exactly the signal an
        # operator needs to notice a misconfigured checkout-session
        # creation step (the other half of this integration).
        self.assertTrue(any("client_reference_id" in record.getMessage() for record in captured.records))

    def test_valid_but_unmatched_reference_is_a_safe_no_op_but_is_logged(self):
        event = {
            "id": "evt_unmatched_ref",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_x",
                    "subscription": "sub_x",
                    "client_reference_id": "11111111-1111-1111-1111-111111111111",
                }
            },
        }
        with self.assertLogs("finance.webhooks", level="WARNING") as captured:
            response = self._post_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(StripeWebhookEvent.objects.filter(event_id="evt_unmatched_ref").exists())
        self.assertTrue(any("does not match any Business" in record.getMessage() for record in captured.records))

    def test_unresolved_subscription_and_invoice_events_are_logged_too(self):
        cases = [
            ({"id": "evt_sub_updated_unmatched", "type": "customer.subscription.updated", "data": {"object": {"id": "sub_ghost"}}}, "subscription"),
            ({"id": "evt_sub_deleted_unmatched", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_ghost2"}}}, "subscription"),
            ({"id": "evt_invoice_paid_unmatched", "type": "invoice.paid", "data": {"object": {"customer": "cus_ghost"}}}, "customer"),
        ]
        for event, _ in cases:
            with self.assertLogs("finance.webhooks", level="WARNING") as captured:
                response = self._post_event(event)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(any("does not match any Business" in record.getMessage() for record in captured.records))


class FinanceTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_fin_iso@example.com")
        self.business_a = Business.objects.create(name="Finance Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Finance Biz B", owner=owner)
        self.customer_b = Customer.objects.create(business=self.business_b, name="Customer B")

        self.user_a = User.objects.create_user(email="staff_fin_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_fin_b@example.com")
        membership_b = BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        self.invoice_b = services.create_invoice(
            self.business_b, self.customer_b, [{"description": "X", "quantity": Decimal("1"), "unit_price": Decimal("10")}], TaxType.ZERO
        )
        services.send_invoice(self.invoice_b)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_invoices(self):
        response = self.client.get(invoice_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_retrieve_other_business_invoice(self):
        response = self.client.get(invoice_detail_url(self.business_b.id, self.invoice_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_record_payment_on_other_business_invoice(self):
        response = self.client.post(
            invoice_record_payment_url(self.business_b.id, self.invoice_b.id), {"amount": "10", "method": "cash"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.invoice_b.refresh_from_db()
        self.assertEqual(self.invoice_b.paid_total, Decimal("0"))

    def test_cannot_create_invoice_for_other_business(self):
        response = self.client.post(
            invoice_list_url(self.business_b.id),
            {
                "customer": str(self.customer_b.id),
                "tax_type": "ZERO",
                "line_items": [{"description": "Sneaky", "quantity": "1", "unit_price": "10"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_list_other_business_payments_or_accounts(self):
        self.assertEqual(self.client.get(payment_list_url(self.business_b.id)).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(account_list_url(self.business_b.id)).status_code, status.HTTP_403_FORBIDDEN)
