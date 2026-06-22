"""
Decision 1 (from the cross-domain audit): Order and Invoice are now
linkable/convertible — services.convert_order_to_invoice, mirroring
finance.services.convert_estimate_to_invoice exactly (same
select_for_update-then-recheck double-guard, same reuse of
finance.services.create_invoice rather than reimplementing invoice
creation). See that function's docstring for the deliberate choice that
Order and Invoice become independent once linked.
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
from finance.models import Invoice

from . import services
from .models import Order

LINE_ITEMS = [
    {"description": "Widget", "quantity": Decimal("2"), "unit_price": Decimal("25.00")},
    {"description": "Gadget", "quantity": Decimal("1"), "unit_price": Decimal("10.00")},
]


def convert_url(business_id, order_id):
    return f"/api/businesses/{business_id}/orders/{order_id}/convert-to-invoice/"


class ConvertOrderToInvoiceTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="convert-order-owner@example.com")
        self.business = Business.objects.create(name="Convert Order Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.order = services.create_order_and_award_points(self.business, self.customer, LINE_ITEMS, "GST_5")

    def test_conversion_creates_an_invoice_with_matching_totals(self):
        invoice = services.convert_order_to_invoice(self.order)

        self.assertEqual(invoice.business_id, self.business.id)
        self.assertEqual(invoice.customer_id, self.customer.id)
        self.assertEqual(invoice.subtotal, self.order.subtotal)
        self.assertEqual(invoice.tax_amount, self.order.tax_amount)
        self.assertEqual(invoice.total, self.order.total)

    def test_conversion_carries_over_line_items_exactly(self):
        invoice = services.convert_order_to_invoice(self.order)

        invoice_items = sorted(
            [(li.description, li.quantity, li.unit_price) for li in invoice.line_items.all()]
        )
        order_items = sorted(
            [(li.description, li.quantity, li.unit_price) for li in self.order.line_items.all()]
        )
        self.assertEqual(invoice_items, order_items)

    def test_conversion_links_the_order_to_the_new_invoice(self):
        invoice = services.convert_order_to_invoice(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.invoice_id, invoice.id)

    def test_cannot_convert_the_same_order_twice(self):
        services.convert_order_to_invoice(self.order)
        with self.assertRaises(services.OrderAlreadyConvertedError):
            services.convert_order_to_invoice(self.order)

    def test_cannot_convert_a_cancelled_order(self):
        services.cancel_order(self.order)
        with self.assertRaises(services.InvalidOrderStateError):
            services.convert_order_to_invoice(self.order)

    def test_cancelling_a_converted_order_does_not_touch_the_invoice(self):
        invoice = services.convert_order_to_invoice(self.order)
        original_status = invoice.status

        services.cancel_order(self.order)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, original_status)
        # The link survives for traceability even after cancellation.
        self.order.refresh_from_db()
        self.assertEqual(self.order.invoice_id, invoice.id)

    def test_a_paid_invoice_is_unaffected_by_cancelling_its_source_order(self):
        invoice = services.convert_order_to_invoice(self.order)
        from finance import services as finance_services

        finance_services.send_invoice(invoice)
        finance_services.record_payment(invoice, invoice.total, "cash")

        services.cancel_order(self.order)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    def test_deleting_the_invoice_nulls_the_orders_link_but_does_not_delete_the_order(self):
        invoice = services.convert_order_to_invoice(self.order)
        invoice.delete()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.invoice_id)
        self.assertTrue(Order.objects.filter(pk=self.order.pk).exists())


class ConvertOrderToInvoiceApiTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="convert-order-api-owner@example.com")
        self.business = Business.objects.create(name="Convert Order API Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.staff_user = User.objects.create_user(email="convert-order-api-staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.order = services.create_order_and_award_points(self.business, self.customer, LINE_ITEMS, "ZERO")
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_convert_endpoint_returns_the_created_invoice(self):
        response = self.client.post(convert_url(self.business.id, self.order.id))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["total"], str(self.order.total))

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.invoice_id)

    def test_convert_endpoint_rejects_double_conversion(self):
        self.client.post(convert_url(self.business.id, self.order.id))
        response = self.client.post(convert_url(self.business.id, self.order.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_convert_another_business_order(self):
        other_owner = User.objects.create_user(email="convert-order-other-owner@example.com")
        other_business = Business.objects.create(name="Other Convert Biz", owner=other_owner)
        BusinessMembership.objects.create(
            business=other_business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        response = self.client.post(convert_url(other_business.id, self.order.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.invoice_id)


class ConvertOrderToInvoiceConcurrencyTests(TransactionTestCase):
    """Two concurrent conversion attempts on the same order — exactly one must succeed, matching the locking discipline used everywhere else in this project."""

    def setUp(self):
        owner = User.objects.create_user(email="convert-order-concurrency@example.com")
        self.business = Business.objects.create(name="Convert Order Concurrency Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.order = services.create_order_and_award_points(self.business, self.customer, LINE_ITEMS, "ZERO")

    def test_only_one_concurrent_conversion_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_convert():
            barrier.wait()
            try:
                services.convert_order_to_invoice(self.order)
                outcome = "success"
            except services.OrderAlreadyConvertedError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_convert) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.order.refresh_from_db()
        self.assertEqual(Invoice.objects.filter(source_orders=self.order).count(), 1)
