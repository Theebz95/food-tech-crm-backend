"""
Invoice/estimate/payment/bill/bank-transaction service layer.

Every status transition (draft -> sent/received -> paid/overdue/cancelled)
and every total recalculation goes through here — never a generic
serializer.save() field write, and never computed live on read. See
models.py module docstring for the Phase 1 audit findings this addresses.

Bill/BillPayment functions deliberately mirror their Invoice/Payment
equivalents structurally (same locking, same overpayment rejection, same
state-machine shape) rather than introducing a different pattern for what
is, mechanically, the same problem (an owed document, payments against
it, a paid-when-covered status) — see record_bill_payment.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    BankTransaction,
    Bill,
    BillLineItem,
    BillNumberSequence,
    BillPayment,
    Estimate,
    EstimateLineItem,
    EstimateNumberSequence,
    Invoice,
    InvoiceLineItem,
    InvoiceNumberSequence,
    Payment,
    Refund,
)
from .tax import TaxLineItem, calculate_totals


class FinanceError(Exception):
    """Base for all domain errors raised by this module. Views translate these to 400s."""


class InvalidInvoiceStateError(FinanceError):
    pass


class InvalidBillStateError(FinanceError):
    pass


class OverpaymentError(FinanceError):
    """Raised by both record_payment (Invoice) and record_bill_payment (Bill) — see either's docstring."""

    def __init__(self, amount, new_total_paid, document_total):
        self.amount = amount
        self.new_total_paid = new_total_paid
        self.document_total = document_total
        super().__init__(
            f"Payment of {amount} would bring total paid to {new_total_paid}, "
            f"exceeding the total of {document_total}."
        )


class InvalidEstimateStateError(FinanceError):
    pass


class RefundError(FinanceError):
    pass


class OverRefundError(RefundError):
    def __init__(self, amount, already_refunded, payment_amount):
        self.amount = amount
        self.already_refunded = already_refunded
        self.payment_amount = payment_amount
        super().__init__(
            f"Cannot refund {amount}; {already_refunded} of {payment_amount} on this payment is already refunded."
        )


def _generate_number(sequence_model, business, prefix) -> str:
    """
    Shared by generate_invoice_number/generate_estimate_number — locks
    and increments inside the caller's transaction (see
    InvoiceNumberSequence's docstring for why a dedicated row, not
    Business itself, is locked).
    """
    sequence, _created = sequence_model.objects.select_for_update().get_or_create(business=business)
    number = sequence.next_number
    sequence.next_number = number + 1
    sequence.save(update_fields=["next_number"])
    return f"{prefix}-{number:05d}"


def generate_invoice_number(business) -> str:
    return _generate_number(InvoiceNumberSequence, business, "INV")


def generate_bill_number(business) -> str:
    return _generate_number(BillNumberSequence, business, "BILL")


def _line_items_for_tax(line_items_data) -> list:
    return [TaxLineItem(quantity=Decimal(str(d["quantity"])), rate=Decimal(str(d["unit_price"]))) for d in line_items_data]


def _apply_totals(document, line_items_data):
    result = calculate_totals(_line_items_for_tax(line_items_data), document.discount_type, document.discount_value, document.tax_type)
    document.subtotal = result.subtotal
    document.discount_amount = result.discount
    document.taxable_amount = result.taxable_amount
    document.tax_amount = result.tax
    document.total = result.total


def create_invoice(
    business,
    customer,
    line_items_data,
    tax_type,
    discount_type="none",
    discount_value=Decimal("0"),
    due_date=None,
    notes="",
    revenue_account=None,
    recurring_transaction=None,
    recurring_occurrence_date=None,
) -> Invoice:
    with transaction.atomic():
        invoice = Invoice(
            business=business,
            customer=customer,
            invoice_number=generate_invoice_number(business),
            tax_type=tax_type,
            discount_type=discount_type,
            discount_value=discount_value,
            due_date=due_date,
            notes=notes,
            revenue_account=revenue_account,
            recurring_transaction=recurring_transaction,
            recurring_occurrence_date=recurring_occurrence_date,
        )
        _apply_totals(invoice, line_items_data)
        invoice.save()
        InvoiceLineItem.objects.bulk_create(
            [
                InvoiceLineItem(
                    invoice=invoice,
                    description=d["description"],
                    quantity=d["quantity"],
                    unit_price=d["unit_price"],
                    sort_order=i,
                )
                for i, d in enumerate(line_items_data)
            ]
        )
        return invoice


def update_invoice(invoice: Invoice, line_items_data=None, **fields) -> Invoice:
    if invoice.status not in (Invoice.Status.DRAFT,):
        raise InvalidInvoiceStateError("Only a draft invoice can be edited. Cancel and re-create otherwise.")

    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        for field, value in fields.items():
            setattr(locked, field, value)

        if line_items_data is not None:
            locked.line_items.all().delete()
            InvoiceLineItem.objects.bulk_create(
                [
                    InvoiceLineItem(
                        invoice=locked,
                        description=d["description"],
                        quantity=d["quantity"],
                        unit_price=d["unit_price"],
                        sort_order=i,
                    )
                    for i, d in enumerate(line_items_data)
                ]
            )
            items_for_tax = line_items_data
        else:
            items_for_tax = [{"quantity": li.quantity, "unit_price": li.unit_price} for li in locked.line_items.all()]

        _apply_totals(locked, items_for_tax)
        locked.save()
        return locked


def send_invoice(invoice: Invoice) -> Invoice:
    if invoice.status != Invoice.Status.DRAFT:
        raise InvalidInvoiceStateError("Only a draft invoice can be sent.")
    invoice.status = Invoice.Status.SENT
    invoice.sent_at = timezone.now()
    invoice.save(update_fields=["status", "sent_at", "updated_at"])
    return invoice


def cancel_invoice(invoice: Invoice) -> Invoice:
    if invoice.status == Invoice.Status.PAID:
        raise InvalidInvoiceStateError("Cannot cancel a fully paid invoice.")
    if invoice.status == Invoice.Status.CANCELLED:
        raise InvalidInvoiceStateError("Invoice is already cancelled.")
    invoice.status = Invoice.Status.CANCELLED
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


def record_payment(invoice: Invoice, amount, method, membership=None, stripe_payment_intent_id="", deposit_account=None, notes="") -> Payment:
    """
    The only place Invoice.status becomes `paid`. Locks the invoice row,
    recomputes the real payment sum (never trusts a client-sent running
    total), and rejects outright — never silently clamps or flags-but-accepts
    — an amount that would overpay. See OverpaymentError.
    """
    with transaction.atomic():
        locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

        if locked_invoice.status == Invoice.Status.CANCELLED:
            raise InvalidInvoiceStateError("Cannot record a payment against a cancelled invoice.")
        if locked_invoice.status == Invoice.Status.DRAFT:
            raise InvalidInvoiceStateError("Invoice must be sent before payments can be recorded.")

        already_paid = locked_invoice.paid_total
        new_total_paid = already_paid + amount
        if new_total_paid > locked_invoice.total:
            raise OverpaymentError(amount, new_total_paid, locked_invoice.total)

        payment = Payment.objects.create(
            business=locked_invoice.business,
            invoice=locked_invoice,
            amount=amount,
            method=method,
            stripe_payment_intent_id=stripe_payment_intent_id,
            deposit_account=deposit_account,
            notes=notes,
            created_by=membership,
        )

        if new_total_paid >= locked_invoice.total:
            locked_invoice.status = Invoice.Status.PAID
            locked_invoice.save(update_fields=["status", "updated_at"])

        return payment


def record_refund(payment: Payment, amount, reason="", membership=None) -> Refund:
    """
    The actual fix for the "refunds deferred to part 2, never built" gap
    documented on Payment/Refund. A Refund is its own append-only ledger
    entry — never an edit to the original Payment, which keeps rejecting
    both .save() on an existing row and .delete() outright.

    Locks the parent Invoice (not the Payment — Payment is already
    immutable; Invoice.status is the thing actually being mutated here),
    so concurrent refund attempts against the same invoice — even against
    different payments on it — serialize correctly through the same lock.
    The refundable amount is checked per-Payment (amount already refunded
    against *this* payment, via its own `refunds` — Refund FKs to Payment,
    not Invoice, so that's the natural unit), exactly mirroring how
    record_payment rejects an overpayment against the invoice total.

    Status recalculation: a refund that brings the invoice's net_paid_total
    (paid minus all refunds) to zero or below moves it to REFUNDED. A
    *partial* refund on a PAID invoice — net_paid_total still positive,
    but now less than the invoice total — reverts it to SENT: there's an
    outstanding balance again, the same state it would be in had it never
    been fully paid. Any other status is left alone (e.g. refunding against
    an invoice that's already REFUNDED, if somehow more than one payment
    contributed, doesn't move it anywhere else).

    No loyalty coupling to reverse: confirmed (see the cross-domain audit)
    that no Invoice/Payment ever triggers loyalty points today — only
    loyalty.Order does. If that ever changes, this is where the reversal
    would need to happen too.
    """
    if amount <= 0:
        raise RefundError("record_refund amount must be positive.")
    if payment.invoice_id is None:
        raise RefundError("Cannot refund a payment that isn't linked to an invoice.")

    with transaction.atomic():
        locked_invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)

        already_refunded = payment.refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        refundable = payment.amount - already_refunded
        if amount > refundable:
            raise OverRefundError(amount, already_refunded, payment.amount)

        refund = Refund.objects.create(
            business=locked_invoice.business, payment=payment, amount=amount, reason=reason, created_by=membership
        )

        net_paid = locked_invoice.net_paid_total
        if net_paid <= 0:
            locked_invoice.status = Invoice.Status.REFUNDED
            locked_invoice.save(update_fields=["status", "updated_at"])
        elif locked_invoice.status == Invoice.Status.PAID and net_paid < locked_invoice.total:
            locked_invoice.status = Invoice.Status.SENT
            locked_invoice.save(update_fields=["status", "updated_at"])

        return refund


def create_bill(
    business,
    vendor,
    line_items_data,
    tax_type,
    discount_type="none",
    discount_value=Decimal("0"),
    due_date=None,
    notes="",
    expense_account=None,
    recurring_transaction=None,
    recurring_occurrence_date=None,
) -> Bill:
    with transaction.atomic():
        bill = Bill(
            business=business,
            vendor=vendor,
            bill_number=generate_bill_number(business),
            tax_type=tax_type,
            discount_type=discount_type,
            discount_value=discount_value,
            due_date=due_date,
            notes=notes,
            expense_account=expense_account,
            recurring_transaction=recurring_transaction,
            recurring_occurrence_date=recurring_occurrence_date,
        )
        _apply_totals(bill, line_items_data)
        bill.save()
        BillLineItem.objects.bulk_create(
            [
                BillLineItem(
                    bill=bill,
                    description=d["description"],
                    quantity=d["quantity"],
                    unit_price=d["unit_price"],
                    sort_order=i,
                )
                for i, d in enumerate(line_items_data)
            ]
        )
        return bill


def update_bill(bill: Bill, line_items_data=None, **fields) -> Bill:
    if bill.status != Bill.Status.DRAFT:
        raise InvalidBillStateError("Only a draft bill can be edited. Cancel and re-create otherwise.")

    with transaction.atomic():
        locked = Bill.objects.select_for_update().get(pk=bill.pk)
        for field, value in fields.items():
            setattr(locked, field, value)

        if line_items_data is not None:
            locked.line_items.all().delete()
            BillLineItem.objects.bulk_create(
                [
                    BillLineItem(
                        bill=locked,
                        description=d["description"],
                        quantity=d["quantity"],
                        unit_price=d["unit_price"],
                        sort_order=i,
                    )
                    for i, d in enumerate(line_items_data)
                ]
            )
            items_for_tax = line_items_data
        else:
            items_for_tax = [{"quantity": li.quantity, "unit_price": li.unit_price} for li in locked.line_items.all()]

        _apply_totals(locked, items_for_tax)
        locked.save()
        return locked


def receive_bill(bill: Bill) -> Bill:
    if bill.status != Bill.Status.DRAFT:
        raise InvalidBillStateError("Only a draft bill can be marked received.")
    bill.status = Bill.Status.RECEIVED
    bill.received_at = timezone.now()
    bill.save(update_fields=["status", "received_at", "updated_at"])
    return bill


def cancel_bill(bill: Bill) -> Bill:
    if bill.status == Bill.Status.PAID:
        raise InvalidBillStateError("Cannot cancel a fully paid bill.")
    if bill.status == Bill.Status.CANCELLED:
        raise InvalidBillStateError("Bill is already cancelled.")
    bill.status = Bill.Status.CANCELLED
    bill.save(update_fields=["status", "updated_at"])
    return bill


def record_bill_payment(
    bill: Bill, amount, method, membership=None, payment_account=None, notes=""
) -> BillPayment:
    """
    The only place Bill.status becomes `paid` — mirrors record_payment
    exactly (locked, recomputed real payment sum, overpayment rejected
    outright). See record_payment's docstring; the reasoning is identical,
    just for money owed to a Vendor instead of money owed by a Customer.
    """
    with transaction.atomic():
        locked_bill = Bill.objects.select_for_update().get(pk=bill.pk)

        if locked_bill.status == Bill.Status.CANCELLED:
            raise InvalidBillStateError("Cannot record a payment against a cancelled bill.")
        if locked_bill.status == Bill.Status.DRAFT:
            raise InvalidBillStateError("Bill must be marked received before payments can be recorded.")

        already_paid = locked_bill.paid_total
        new_total_paid = already_paid + amount
        if new_total_paid > locked_bill.total:
            raise OverpaymentError(amount, new_total_paid, locked_bill.total)

        bill_payment = BillPayment.objects.create(
            business=locked_bill.business,
            bill=locked_bill,
            amount=amount,
            method=method,
            payment_account=payment_account,
            notes=notes,
            created_by=membership,
        )

        if new_total_paid >= locked_bill.total:
            locked_bill.status = Bill.Status.PAID
            locked_bill.save(update_fields=["status", "updated_at"])

        return bill_payment


# Models a BankTransaction may be reconciled against — see
# BankTransaction.reconciled_object's docstring for why this is an
# explicit allow-list rather than an unrestricted GenericForeignKey.
RECONCILIATION_MODELS = {
    "invoice": Invoice,
    "payment": Payment,
    "bill": Bill,
    "billpayment": BillPayment,
}


def reconcile_bank_transaction(bank_transaction: BankTransaction, target_object) -> BankTransaction:
    if getattr(target_object, "business_id", None) != bank_transaction.business_id:
        raise FinanceError("Reconciliation target does not belong to this business.")
    bank_transaction.reconciled_object = target_object
    bank_transaction.is_reconciled = True
    bank_transaction.save(
        update_fields=["reconciled_content_type", "reconciled_object_id", "is_reconciled", "updated_at"]
    )
    return bank_transaction


def generate_estimate_number(business) -> str:
    return _generate_number(EstimateNumberSequence, business, "EST")


def create_estimate(business, customer, line_items_data, tax_type, discount_type="none", discount_value=Decimal("0"), expires_at=None, notes="") -> Estimate:
    with transaction.atomic():
        estimate = Estimate(
            business=business,
            customer=customer,
            estimate_number=generate_estimate_number(business),
            tax_type=tax_type,
            discount_type=discount_type,
            discount_value=discount_value,
            expires_at=expires_at,
            notes=notes,
        )
        _apply_totals(estimate, line_items_data)
        estimate.save()
        EstimateLineItem.objects.bulk_create(
            [
                EstimateLineItem(
                    estimate=estimate,
                    description=d["description"],
                    quantity=d["quantity"],
                    unit_price=d["unit_price"],
                    sort_order=i,
                )
                for i, d in enumerate(line_items_data)
            ]
        )
        return estimate


def update_estimate(estimate: Estimate, line_items_data=None, **fields) -> Estimate:
    if estimate.status == Estimate.Status.CONVERTED:
        raise InvalidEstimateStateError("Cannot edit an estimate that has already been converted.")

    with transaction.atomic():
        locked = Estimate.objects.select_for_update().get(pk=estimate.pk)
        if locked.status == Estimate.Status.CONVERTED:
            raise InvalidEstimateStateError("Cannot edit an estimate that has already been converted.")

        for field, value in fields.items():
            setattr(locked, field, value)

        if line_items_data is not None:
            locked.line_items.all().delete()
            EstimateLineItem.objects.bulk_create(
                [
                    EstimateLineItem(
                        estimate=locked,
                        description=d["description"],
                        quantity=d["quantity"],
                        unit_price=d["unit_price"],
                        sort_order=i,
                    )
                    for i, d in enumerate(line_items_data)
                ]
            )
            items_for_tax = line_items_data
        else:
            items_for_tax = [{"quantity": li.quantity, "unit_price": li.unit_price} for li in locked.line_items.all()]

        _apply_totals(locked, items_for_tax)
        locked.save()
        return locked


def convert_estimate_to_invoice(estimate: Estimate, due_date=None) -> Invoice:
    if estimate.status == Estimate.Status.CONVERTED:
        raise InvalidEstimateStateError("Estimate has already been converted.")

    with transaction.atomic():
        locked_estimate = Estimate.objects.select_for_update().get(pk=estimate.pk)
        if locked_estimate.status == Estimate.Status.CONVERTED:
            raise InvalidEstimateStateError("Estimate has already been converted.")

        line_items_data = [
            {"description": li.description, "quantity": li.quantity, "unit_price": li.unit_price}
            for li in locked_estimate.line_items.all()
        ]
        invoice = create_invoice(
            business=locked_estimate.business,
            customer=locked_estimate.customer,
            line_items_data=line_items_data,
            tax_type=locked_estimate.tax_type,
            discount_type=locked_estimate.discount_type,
            discount_value=locked_estimate.discount_value,
            due_date=due_date,
            notes=locked_estimate.notes,
        )

        locked_estimate.status = Estimate.Status.CONVERTED
        locked_estimate.converted_invoice = invoice
        locked_estimate.save(update_fields=["status", "converted_invoice", "updated_at"])
        return invoice
