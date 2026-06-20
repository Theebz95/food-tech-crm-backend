"""
Tests for Finance part 2: bills/bill payments (mirroring part 1's
Invoice/Payment rigor exactly), bank transaction reconciliation, recurring
transaction expansion (idempotency, reusing core.recurrence), AR/AP aging
report bucketing against hand-calculated due dates, and tenant isolation.
"""

import threading
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership
from customers.models import Customer
from inventory.models import Vendor

from . import services
from .models import (
    BankTransaction,
    Bill,
    BillPayment,
    ChartOfAccount,
    Invoice,
    Payment,
    RecurringTransaction,
)
from .recurring import expand_active_recurring_transactions, expand_recurring_transaction
from .reports import ap_aging_report, ar_aging_report
from .tax import TaxType


def bill_list_url(business_id):
    return f"/api/businesses/{business_id}/bills/"


def bill_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/bills/{pk}/"


def bill_record_payment_url(business_id, pk):
    return f"/api/businesses/{business_id}/bills/{pk}/record-payment/"


def bank_transaction_list_url(business_id):
    return f"/api/businesses/{business_id}/bank-transactions/"


def bank_transaction_reconcile_url(business_id, pk):
    return f"/api/businesses/{business_id}/bank-transactions/{pk}/reconcile/"


def recurring_transaction_list_url(business_id):
    return f"/api/businesses/{business_id}/recurring-transactions/"


def ar_aging_url(business_id):
    return f"/api/businesses/{business_id}/reports/ar-aging/"


def ap_aging_url(business_id):
    return f"/api/businesses/{business_id}/reports/ap-aging/"


SAMPLE_LINE_ITEMS = [{"description": "Service", "quantity": Decimal("1"), "unit_price": Decimal("100")}]


class BillCreationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_bill@example.com")
        self.business = Business.objects.create(name="Bill Biz", owner=owner)
        self.vendor = Vendor.objects.create(business=self.business, name="Acme Supply")
        self.staff_user = User.objects.create_user(email="staff_bill@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_bill_computes_totals(self):
        response = self.client.post(
            bill_list_url(self.business.id),
            {
                "vendor": str(self.vendor.id),
                "tax_type": "GST_5",
                "line_items": [{"description": "Supplies", "quantity": "2", "unit_price": "50.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["subtotal"], "100.00")
        self.assertEqual(response.data["tax_amount"], "5.00")
        self.assertEqual(response.data["total"], "105.00")
        self.assertEqual(response.data["status"], "draft")
        self.assertTrue(response.data["bill_number"].startswith("BILL-"))

    def test_bill_numbers_are_sequential_and_independent_of_invoice_numbers(self):
        first = services.create_bill(self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        second = services.create_bill(self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        self.assertEqual(first.bill_number, "BILL-00001")
        self.assertEqual(second.bill_number, "BILL-00002")

    def test_cannot_use_a_vendor_from_another_business(self):
        other_owner = User.objects.create_user(email="other_owner_bill@example.com")
        other_business = Business.objects.create(name="Other Bill Biz", owner=other_owner)
        other_vendor = Vendor.objects.create(business=other_business, name="Other Vendor")
        response = self.client.post(
            bill_list_url(self.business.id),
            {"vendor": str(other_vendor.id), "tax_type": "ZERO", "line_items": SAMPLE_LINE_ITEMS},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BillPaymentTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_billpay@example.com")
        self.business = Business.objects.create(name="Bill Payment Biz", owner=owner)
        self.vendor = Vendor.objects.create(business=self.business, name="Vendor")
        self.membership = BusinessMembership.objects.create(
            business=self.business,
            user=User.objects.create_user(email="staff_billpay@example.com"),
            role=BusinessMembership.Role.STAFF,
        )
        self.bill = services.create_bill(self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        services.receive_bill(self.bill)

    def test_full_payment_marks_bill_paid(self):
        services.record_bill_payment(self.bill, Decimal("100"), BillPayment.Method.CASH, self.membership)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, Bill.Status.PAID)

    def test_partial_payment_does_not_mark_paid(self):
        services.record_bill_payment(self.bill, Decimal("40"), BillPayment.Method.CASH, self.membership)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, Bill.Status.RECEIVED)
        self.assertEqual(self.bill.paid_total, Decimal("40"))

    def test_overpayment_is_rejected(self):
        with self.assertRaises(services.OverpaymentError):
            services.record_bill_payment(self.bill, Decimal("150"), BillPayment.Method.CASH, self.membership)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, Bill.Status.RECEIVED)
        self.assertEqual(BillPayment.objects.filter(bill=self.bill).count(), 0)

    def test_cannot_pay_a_draft_bill(self):
        draft_bill = services.create_bill(self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        with self.assertRaises(services.InvalidBillStateError):
            services.record_bill_payment(draft_bill, Decimal("10"), BillPayment.Method.CASH, self.membership)

    def test_cannot_pay_a_cancelled_bill(self):
        services.cancel_bill(self.bill)
        with self.assertRaises(services.InvalidBillStateError):
            services.record_bill_payment(self.bill, Decimal("10"), BillPayment.Method.CASH, self.membership)

    def test_bill_payments_are_append_only(self):
        payment = services.record_bill_payment(self.bill, Decimal("100"), BillPayment.Method.CASH, self.membership)
        payment.notes = "tampered"
        with self.assertRaises(TypeError):
            payment.save()
        with self.assertRaises(TypeError):
            payment.delete()


class BillPaymentConcurrencyTests(TransactionTestCase):
    """Same shape as finance.tests.PaymentConcurrencyTests — bill total=100, two concurrent payments of 80."""

    def setUp(self):
        owner = User.objects.create_user(email="owner_bill_concurrency@example.com")
        self.business = Business.objects.create(name="Bill Concurrency Co", owner=owner)
        self.vendor = Vendor.objects.create(business=self.business, name="Concurrent Vendor")
        user = User.objects.create_user(email="staff_bill_concurrency@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.bill = services.create_bill(self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        services.receive_bill(self.bill)

    def test_only_one_concurrent_bill_payment_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_payment():
            barrier.wait()
            try:
                services.record_bill_payment(self.bill, Decimal("80"), BillPayment.Method.CASH, self.membership)
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
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.paid_total, Decimal("80"))
        self.assertEqual(BillPayment.objects.filter(bill=self.bill).count(), 1)


class BankTransactionReconciliationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_bank@example.com")
        self.business = Business.objects.create(name="Bank Biz", owner=owner)
        self.other_business = Business.objects.create(name="Other Bank Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.staff_user = User.objects.create_user(email="staff_bank@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.invoice = services.create_invoice(self.business, self.customer, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        services.send_invoice(self.invoice)
        self.bank_transaction = BankTransaction.objects.create(
            business=self.business, date=timezone.now().date(), description="Deposit", amount=Decimal("100")
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_default_source_is_manual(self):
        self.assertEqual(self.bank_transaction.source, BankTransaction.Source.MANUAL)
        self.assertEqual(self.bank_transaction.external_transaction_id, "")

    def test_reconcile_to_an_invoice(self):
        response = self.client.post(
            bank_transaction_reconcile_url(self.business.id, self.bank_transaction.id),
            {"target_type": "invoice", "object_id": str(self.invoice.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.bank_transaction.refresh_from_db()
        self.assertTrue(self.bank_transaction.is_reconciled)
        self.assertEqual(self.bank_transaction.reconciled_object, self.invoice)

    def test_cannot_reconcile_to_an_object_from_another_business(self):
        other_customer = Customer.objects.create(business=self.other_business, name="Other Customer")
        other_invoice = services.create_invoice(self.other_business, other_customer, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        response = self.client.post(
            bank_transaction_reconcile_url(self.business.id, self.bank_transaction.id),
            {"target_type": "invoice", "object_id": str(other_invoice.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.bank_transaction.refresh_from_db()
        self.assertFalse(self.bank_transaction.is_reconciled)

    def test_cannot_reconcile_to_a_nonexistent_object(self):
        import uuid

        response = self.client.post(
            bank_transaction_reconcile_url(self.business.id, self.bank_transaction.id),
            {"target_type": "invoice", "object_id": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_reconcile_to_a_disallowed_model(self):
        response = self.client.post(
            bank_transaction_reconcile_url(self.business.id, self.bank_transaction.id),
            {"target_type": "customer", "object_id": str(self.customer.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RecurringTransactionExpansionTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_recurring@example.com")
        self.business = Business.objects.create(name="Recurring Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Recurring Customer")
        self.vendor = Vendor.objects.create(business=self.business, name="Recurring Vendor")

    def test_weekly_invoice_expansion_creates_one_invoice_per_week(self):
        start = timezone.now().date()
        rt = RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=self.customer,
            line_item_presets=[{"description": "Weekly service", "quantity": 1, "unit_price": 100}],
            recurrence_rule=RecurringTransaction.Recurrence.WEEKLY,
            start_date=start,
        )
        created = expand_recurring_transaction(rt, start, start + timezone.timedelta(days=20))
        self.assertEqual(created, 3)  # day 0, 7, 14 within a 20-day window
        invoices = Invoice.objects.filter(recurring_transaction=rt)
        self.assertEqual(invoices.count(), 3)
        for invoice in invoices:
            self.assertEqual(invoice.total, Decimal("100.00"))
            self.assertEqual(invoice.due_date, invoice.recurring_occurrence_date + timezone.timedelta(days=30))

    def test_monthly_bill_expansion(self):
        start = timezone.now().date().replace(day=1)
        rt = RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.BILL,
            vendor=self.vendor,
            line_item_presets=[{"description": "Monthly rent", "quantity": 1, "unit_price": 500}],
            recurrence_rule=RecurringTransaction.Recurrence.MONTHLY,
            start_date=start,
        )
        created = expand_recurring_transaction(rt, start, start + timezone.timedelta(days=95))
        self.assertEqual(created, 4)  # months 0,1,2,3 inclusive of the +95 day window
        self.assertEqual(Bill.objects.filter(recurring_transaction=rt).count(), 4)

    def test_running_expansion_twice_does_not_duplicate(self):
        start = timezone.now().date()
        rt = RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=self.customer,
            line_item_presets=[{"description": "Weekly service", "quantity": 1, "unit_price": 100}],
            recurrence_rule=RecurringTransaction.Recurrence.WEEKLY,
            start_date=start,
        )
        window_end = start + timezone.timedelta(days=20)
        first_run = expand_recurring_transaction(rt, start, window_end)
        second_run = expand_recurring_transaction(rt, start, window_end)
        self.assertEqual(first_run, 3)
        self.assertEqual(second_run, 0)
        self.assertEqual(Invoice.objects.filter(recurring_transaction=rt).count(), 3)

    def test_inactive_recurring_transaction_is_not_expanded_by_the_rolling_task(self):
        start = timezone.now().date()
        RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=self.customer,
            line_item_presets=[{"description": "Paused service", "quantity": 1, "unit_price": 100}],
            recurrence_rule=RecurringTransaction.Recurrence.WEEKLY,
            start_date=start,
            is_active=False,
        )
        created = expand_active_recurring_transactions(window_days=28)
        self.assertEqual(created, 0)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_active_recurring_transaction_is_expanded_by_the_rolling_task(self):
        start = timezone.now().date()
        RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=self.customer,
            line_item_presets=[{"description": "Active service", "quantity": 1, "unit_price": 100}],
            recurrence_rule=RecurringTransaction.Recurrence.WEEKLY,
            start_date=start,
        )
        created = expand_active_recurring_transactions(window_days=28)
        self.assertGreaterEqual(created, 1)
        self.assertGreaterEqual(Invoice.objects.count(), 1)


class RecurringTransactionSerializerValidationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_rt_validation@example.com")
        self.business = Business.objects.create(name="RT Validation Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.staff_user = User.objects.create_user(email="staff_rt_validation@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_invoice_kind_requires_customer(self):
        response = self.client.post(
            recurring_transaction_list_url(self.business.id),
            {
                "kind": "invoice",
                "line_item_presets": [{"description": "X", "quantity": 1, "unit_price": 10}],
                "recurrence_rule": "weekly",
                "start_date": timezone.now().date().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invoice_kind_rejects_a_vendor_being_set(self):
        vendor = Vendor.objects.create(business=self.business, name="Vendor")
        response = self.client.post(
            recurring_transaction_list_url(self.business.id),
            {
                "kind": "invoice",
                "customer": str(self.customer.id),
                "vendor": str(vendor.id),
                "line_item_presets": [{"description": "X", "quantity": 1, "unit_price": 10}],
                "recurrence_rule": "weekly",
                "start_date": timezone.now().date().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_invoice_recurring_transaction_created(self):
        response = self.client.post(
            recurring_transaction_list_url(self.business.id),
            {
                "kind": "invoice",
                "customer": str(self.customer.id),
                "line_item_presets": [{"description": "X", "quantity": 1, "unit_price": 10}],
                "recurrence_rule": "weekly",
                "start_date": timezone.now().date().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class AgingReportTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_aging@example.com")
        self.business = Business.objects.create(name="Aging Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Aging Customer")
        self.vendor = Vendor.objects.create(business=self.business, name="Aging Vendor")
        self.staff_user = User.objects.create_user(email="staff_aging@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.today = timezone.now().date()
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def _sent_invoice(self, due_date, total=Decimal("100")):
        invoice = services.create_invoice(
            self.business, self.customer, [{"description": "X", "quantity": Decimal("1"), "unit_price": total}],
            TaxType.ZERO, due_date=due_date,
        )
        services.send_invoice(invoice)
        return invoice

    def test_hand_calculated_buckets(self):
        current_invoice = self._sent_invoice(due_date=self.today + timezone.timedelta(days=5))
        bucket_1_30 = self._sent_invoice(due_date=self.today - timezone.timedelta(days=10))
        bucket_31_60 = self._sent_invoice(due_date=self.today - timezone.timedelta(days=45))
        bucket_61_90 = self._sent_invoice(due_date=self.today - timezone.timedelta(days=75))
        bucket_90_plus = self._sent_invoice(due_date=self.today - timezone.timedelta(days=120))

        report = ar_aging_report(self.business, as_of=self.today)

        self.assertEqual(report.bucket_counts["current"], 1)
        self.assertEqual(report.bucket_counts["1_30"], 1)
        self.assertEqual(report.bucket_counts["31_60"], 1)
        self.assertEqual(report.bucket_counts["61_90"], 1)
        self.assertEqual(report.bucket_counts["90_plus"], 1)
        self.assertEqual(report.bucket_totals["current"], Decimal("100.00"))
        self.assertEqual(report.grand_total, Decimal("500.00"))

        self.assertIn(current_invoice, report.bucket_rows["current"])
        self.assertIn(bucket_1_30, report.bucket_rows["1_30"])
        self.assertIn(bucket_31_60, report.bucket_rows["31_60"])
        self.assertIn(bucket_61_90, report.bucket_rows["61_90"])
        self.assertIn(bucket_90_plus, report.bucket_rows["90_plus"])

    def test_paid_invoices_excluded_from_aging(self):
        invoice = self._sent_invoice(due_date=self.today - timezone.timedelta(days=10))
        services.record_payment(invoice, invoice.total, Payment.Method.CASH)
        report = ar_aging_report(self.business, as_of=self.today)
        self.assertEqual(sum(report.bucket_counts.values()), 0)

    def test_draft_invoices_excluded_from_aging(self):
        services.create_invoice(
            self.business, self.customer, SAMPLE_LINE_ITEMS, TaxType.ZERO,
            due_date=self.today - timezone.timedelta(days=10),
        )
        report = ar_aging_report(self.business, as_of=self.today)
        self.assertEqual(sum(report.bucket_counts.values()), 0)

    def test_ap_aging_mirrors_ar_for_bills(self):
        bill = services.create_bill(
            self.business, self.vendor, SAMPLE_LINE_ITEMS, TaxType.ZERO,
            due_date=self.today - timezone.timedelta(days=45),
        )
        services.receive_bill(bill)
        report = ap_aging_report(self.business, as_of=self.today)
        self.assertEqual(report.bucket_counts["31_60"], 1)
        self.assertIn(bill, report.bucket_rows["31_60"])

    def test_summary_endpoint_returns_buckets_without_pagination(self):
        self._sent_invoice(due_date=self.today - timezone.timedelta(days=10))
        response = self.client.get(ar_aging_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["buckets"]["1_30"]["count"], 1)
        self.assertNotIn("results", response.data)

    def test_bucket_detail_endpoint_is_paginated(self):
        for _ in range(3):
            self._sent_invoice(due_date=self.today - timezone.timedelta(days=10))
        response = self.client.get(ar_aging_url(self.business.id), {"bucket": "1_30"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)
        self.assertIn("count", response.data)
        self.assertEqual(response.data["count"], 3)

    def test_invalid_bucket_param_rejected(self):
        response = self.client.get(ar_aging_url(self.business.id), {"bucket": "not-a-real-bucket"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class FinancePart2TenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_fin2_iso@example.com")
        self.business_a = Business.objects.create(name="Fin2 Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Fin2 Biz B", owner=owner)
        self.vendor_b = Vendor.objects.create(business=self.business_b, name="Vendor B")

        self.user_a = User.objects.create_user(email="staff_fin2_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_fin2_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        self.bill_b = services.create_bill(self.business_b, self.vendor_b, SAMPLE_LINE_ITEMS, TaxType.ZERO)
        services.receive_bill(self.bill_b)
        self.bank_transaction_b = BankTransaction.objects.create(
            business=self.business_b, date=timezone.now().date(), description="X", amount=Decimal("10")
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_bills(self):
        response = self.client.get(bill_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_retrieve_other_business_bill(self):
        response = self.client.get(bill_detail_url(self.business_b.id, self.bill_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_record_payment_on_other_business_bill(self):
        response = self.client.post(
            bill_record_payment_url(self.business_b.id, self.bill_b.id), {"amount": "10", "method": "cash"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.bill_b.refresh_from_db()
        self.assertEqual(self.bill_b.paid_total, Decimal("0"))

    def test_cannot_list_other_business_bank_transactions(self):
        response = self.client.get(bank_transaction_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_reconcile_other_business_bank_transaction(self):
        response = self.client.post(
            bank_transaction_reconcile_url(self.business_b.id, self.bank_transaction_b.id),
            {"target_type": "bill", "object_id": str(self.bill_b.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.bank_transaction_b.refresh_from_db()
        self.assertFalse(self.bank_transaction_b.is_reconciled)

    def test_cannot_list_other_business_recurring_transactions(self):
        response = self.client.get(recurring_transaction_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_view_other_business_aging_reports(self):
        self.assertEqual(self.client.get(ar_aging_url(self.business_b.id)).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(ap_aging_url(self.business_b.id)).status_code, status.HTTP_403_FORBIDDEN)
