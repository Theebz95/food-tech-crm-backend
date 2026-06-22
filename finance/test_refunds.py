"""
Decision 3 (from the cross-domain audit): a real refund/reversal
mechanism for Payment, replacing the "deferred to part 2, never built"
gap. See models.py:Refund and services.py:record_refund for the full
design — same append-only-ledger and select_for_update discipline as
every other money operation in this project (Payment itself,
loyalty.PointsTransaction/GiftCardTransaction, inventory.InventoryTransaction).
"""

import threading
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership
from customers.models import Customer

from . import services
from .models import Invoice, Refund

LINE_ITEMS = [{"description": "Widget", "quantity": Decimal("1"), "unit_price": Decimal("100.00")}]


def refund_url(business_id, payment_id):
    return f"/api/businesses/{business_id}/payments/{payment_id}/refund/"


def refund_list_url(business_id):
    return f"/api/businesses/{business_id}/refunds/"


class RecordRefundTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="refund-record-owner@example.com")
        self.business = Business.objects.create(name="Refund Record Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.invoice = services.create_invoice(self.business, self.customer, LINE_ITEMS, "ZERO")
        services.send_invoice(self.invoice)
        self.payment = services.record_payment(self.invoice, Decimal("100.00"), "cash")
        self.invoice.refresh_from_db()

    def test_invoice_is_paid_before_any_refund(self):
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(self.invoice.paid_total, Decimal("100.00"))
        self.assertEqual(self.invoice.refunded_total, Decimal("0"))
        self.assertEqual(self.invoice.net_paid_total, Decimal("100.00"))

    def test_full_refund_moves_invoice_to_refunded(self):
        services.record_refund(self.payment, Decimal("100.00"), reason="Customer changed mind")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.REFUNDED)
        self.assertEqual(self.invoice.net_paid_total, Decimal("0"))

    def test_partial_refund_reverts_a_paid_invoice_to_sent(self):
        services.record_refund(self.payment, Decimal("30.00"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.SENT)
        self.assertEqual(self.invoice.refunded_total, Decimal("30.00"))
        self.assertEqual(self.invoice.net_paid_total, Decimal("70.00"))

    def test_cumulative_partial_refunds_are_tracked_correctly(self):
        services.record_refund(self.payment, Decimal("30.00"))
        services.record_refund(self.payment, Decimal("20.00"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.refunded_total, Decimal("50.00"))
        self.assertEqual(self.invoice.net_paid_total, Decimal("50.00"))
        self.assertEqual(self.invoice.status, Invoice.Status.SENT)

    def test_cumulative_refunds_reaching_the_full_amount_moves_to_refunded(self):
        services.record_refund(self.payment, Decimal("60.00"))
        services.record_refund(self.payment, Decimal("40.00"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.REFUNDED)

    def test_over_refund_against_a_single_payment_is_rejected(self):
        services.record_refund(self.payment, Decimal("60.00"))
        with self.assertRaises(services.OverRefundError):
            services.record_refund(self.payment, Decimal("41.00"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.refunded_total, Decimal("60.00"))

    def test_refund_amount_must_be_positive(self):
        with self.assertRaises(services.RefundError):
            services.record_refund(self.payment, Decimal("0"))
        with self.assertRaises(services.RefundError):
            services.record_refund(self.payment, Decimal("-10"))

    def test_cannot_refund_a_payment_with_no_invoice(self):
        """Simulates a Payment whose Invoice was since deleted (invoice FK is SET_NULL, see models.py)."""
        self.payment.invoice_id = None
        with self.assertRaises(services.RefundError):
            services.record_refund(self.payment, Decimal("10.00"))

    def test_refund_creates_an_append_only_ledger_entry(self):
        refund = services.record_refund(self.payment, Decimal("25.00"), reason="Partial return")
        self.assertEqual(refund.amount, Decimal("25.00"))
        self.assertEqual(refund.reason, "Partial return")
        self.assertEqual(refund.business_id, self.business.id)
        self.assertEqual(refund.payment_id, self.payment.id)

        with self.assertRaises(TypeError):
            refund.amount = Decimal("999")
            refund.save()
        with self.assertRaises(TypeError):
            refund.delete()

    def test_payment_itself_is_unaffected_by_being_refunded(self):
        """The Payment row never changes — a Refund is a separate row, never an edit to it."""
        original_amount = self.payment.amount
        services.record_refund(self.payment, Decimal("25.00"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, original_amount)


class RefundConcurrencyTests(TransactionTestCase):
    """Two concurrent refund attempts against the same payment, both for the full $100 — exactly one must succeed."""

    def setUp(self):
        owner = User.objects.create_user(email="refund-concurrency-owner@example.com")
        self.business = Business.objects.create(name="Refund Concurrency Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.invoice = services.create_invoice(self.business, self.customer, LINE_ITEMS, "ZERO")
        services.send_invoice(self.invoice)
        self.payment = services.record_payment(self.invoice, Decimal("100.00"), "cash")

    def test_only_one_concurrent_full_refund_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_refund():
            barrier.wait()
            try:
                services.record_refund(self.payment, Decimal("100.00"))
                outcome = "success"
            except services.OverRefundError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_refund) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.refunded_total, Decimal("100.00"))
        self.assertGreaterEqual(self.payment.amount - self.invoice.refunded_total, Decimal("0"))


class RefundApiTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="refund-api-owner@example.com")
        self.business = Business.objects.create(name="Refund API Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.invoice = services.create_invoice(self.business, self.customer, LINE_ITEMS, "ZERO")
        services.send_invoice(self.invoice)
        self.payment = services.record_payment(self.invoice, Decimal("100.00"), "cash")

        self.staff_user = User.objects.create_user(email="refund-api-staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_refund_action_succeeds_and_returns_updated_invoice(self):
        response = self.client.post(
            refund_url(self.business.id, self.payment.id), {"amount": "40.00", "reason": "Damaged item"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["refund"]["amount"], "40.00")
        self.assertEqual(response.data["invoice"]["status"], Invoice.Status.SENT)
        self.assertEqual(response.data["invoice"]["refunded_total"], "40.00")

    def test_refund_action_rejects_over_refund_with_400(self):
        response = self.client.post(refund_url(self.business.id, self.payment.id), {"amount": "150.00"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refund_list_endpoint_returns_created_refunds(self):
        self.client.post(refund_url(self.business.id, self.payment.id), {"amount": "10.00"})
        response = self.client.get(refund_list_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_refund_list_endpoint_does_not_support_create(self):
        response = self.client.post(refund_list_url(self.business.id), {"amount": "10.00"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class RefundTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="refund-iso-owner@example.com")
        self.business_a = Business.objects.create(name="Refund Iso A", owner=owner)
        self.business_b = Business.objects.create(name="Refund Iso B", owner=owner)
        customer_b = Customer.objects.create(business=self.business_b, name="Customer B")
        invoice_b = services.create_invoice(self.business_b, customer_b, LINE_ITEMS, "ZERO")
        services.send_invoice(invoice_b)
        self.payment_b = services.record_payment(invoice_b, Decimal("100.00"), "cash")
        services.record_refund(self.payment_b, Decimal("10.00"))

        self.staff_a = User.objects.create_user(email="refund-iso-staff-a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.staff_a, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_a)

    def test_cannot_refund_another_business_payment(self):
        response = self.client.post(refund_url(self.business_b.id, self.payment_b.id), {"amount": "10.00"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_list_another_business_refunds(self):
        response = self.client.get(refund_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Refund.objects.filter(business=self.business_b).count(), 1)
