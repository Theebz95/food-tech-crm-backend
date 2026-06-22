"""
Finance domain — now complete across both parts.

Part 1: invoicing, payments, estimates, and the Stripe webhook.
Part 2 (this addition): chart of accounts (already minimal-built in part
1), bills/bill payments, bank transactions (manual entry only — see
BankTransaction docstring), recurring transactions, and AR/AP aging
reports.

  invoices              (old) -> Invoice
  invoice_line_items    (old) -> InvoiceLineItem
  payments              (old) -> Payment
  estimates             (old) -> Estimate
  estimate_line_items   (old) -> EstimateLineItem
  invoice_templates     (old) -> InvoiceTemplate
  chart_of_accounts     (old) -> ChartOfAccount (minimal — see docstring below)
  bills                 (old) -> Bill
  bill_line_items       (old) -> BillLineItem
  bill_payments         (old) -> BillPayment
  bank_transactions     (old) -> BankTransaction (manual-entry only for now)
  recurring_transactions(old) -> RecurringTransaction
  (no old equivalent)   -> InvoiceNumberSequence, EstimateNumberSequence,
                           BillNumberSequence, StripeWebhookEvent

Tax calculation (Phase 1 audit finding) is a direct port of the old
frontend's tax-utils.ts — see finance/tax.py for the original source and
exactly what changed (only Decimal precision, never the rates/algorithm).
Tax is computed once per document (`Invoice`/`Bill`'s `tax_type`/`discount_type`/
`discount_value`), not per line item — that's what the ported source
actually does; line items only carry quantity/unit_price.

The actual fixes (Phase 1 audit findings):

  1. Invoice/payment status was ad hoc client-side. `Invoice.status` now
     only ever changes via finance/services.py: `record_payment` sets
     `paid` (atomically, recalculated from the real payment sum — never
     trusted from the client), and the daily `mark_overdue_invoices`
     Celery task sets `overdue` from `due_date` — never computed live on
     read. `Bill`/`record_bill_payment`/`mark_overdue_bills` mirror this
     exactly. See services.py / tasks.py.

  2. The Stripe webhook (webhooks.py) was a real functional gap, not a
     porting task — checkout completion never synced back to the
     database at all in the old system. Built here with signature
     verification and idempotency (`StripeWebhookEvent` — see webhooks.py
     for why event-row-insert and handler side effects share one
     transaction).

  3. Recurring transaction generation was manually triggered from the
     frontend, with no real scheduler. `RecurringTransaction` expansion
     (finance/recurring.py) reuses the exact same idempotent
     rolling-window Celery Beat pattern already proven for
     `employees.RecurringSchedule` -> `EmployeeShift` — including the
     actual date-stepping logic itself, extracted to `core/recurrence.py`
     specifically so it's shared rather than re-implemented.

  4. AR/AP aging reports were computed by loading every row client-side,
     with no pagination. `finance/reports.py` computes the bucket
     aggregation server-side; the bucket summary itself is small and
     unpaginated by design (5 buckets), but the underlying per-bucket
     detail list (`?bucket=...`) is paginated — see "AR/AP aging
     reports" in README "Finance domain" for the exact endpoint shape.
"""

import uuid
from decimal import Decimal

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator
from django.db import models

from core.models import Business, BusinessMembership
from customers.models import Customer
from inventory.models import Vendor

from .tax import DiscountType, TaxType


class ChartOfAccount(models.Model):
    """
    Minimal version — just enough for Invoice/Payment to optionally
    reference an account without blocking this part. The full buildout
    (hierarchical accounts, balances, journal entries) is part 2.
    """

    class AccountType(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="chart_of_accounts")
    code = models.CharField(max_length=32, blank=True, default="")
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_account_name_per_business"),
        ]
        ordering = ["business", "code", "name"]

    def __str__(self):
        return f"{self.code + ' ' if self.code else ''}{self.name}"


class InvoiceNumberSequence(models.Model):
    """
    One counter per business, locked (`select_for_update()`) and
    incremented inside the same transaction as the Invoice it numbers
    (finance/services.py:generate_invoice_number) — deliberately a tiny
    dedicated row rather than locking `Business` itself, so invoice
    numbering contention never blocks unrelated operations elsewhere in
    the codebase that also touch a Business row.

    A failed invoice creation after the counter increments leaves a gap
    in the numbering (e.g. 1003 never gets used) rather than rolling the
    counter back — an accepted, standard tradeoff for sequential
    numbering under concurrency, not a bug.
    """

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="invoice_number_sequence")
    next_number = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"Invoice sequence for {self.business} (next: {self.next_number})"


class EstimateNumberSequence(models.Model):
    """Same shape and reasoning as InvoiceNumberSequence, kept separate so invoice and estimate numbering don't share one counter."""

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="estimate_number_sequence")
    next_number = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"Estimate sequence for {self.business} (next: {self.next_number})"


class BillNumberSequence(models.Model):
    """Same shape and reasoning as InvoiceNumberSequence, kept separate so bill numbering has its own counter."""

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="bill_number_sequence")
    next_number = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"Bill sequence for {self.business} (next: {self.next_number})"


class RecurringTransaction(models.Model):
    """
    Generates real Invoice or Bill rows on a schedule — see
    finance/recurring.py for the expansion logic (mirrors
    employees.scheduling's RecurringSchedule -> EmployeeShift pattern,
    reusing the shared date math in core/recurrence.py rather than
    reimplementing it).

    Carries its own embedded line-item spec (`line_item_presets`) and
    tax/discount fields rather than FK'ing to InvoiceTemplate: there's no
    equivalent "BillTemplate" model, and FK'ing the invoice case to
    InvoiceTemplate while the bill case had to embed its own spec anyway
    would make the two kinds of this one model asymmetric for no real
    benefit. One shape works identically for both `kind`s.
    """

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        BILL = "bill", "Bill"

    class Recurrence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="recurring_transactions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Exactly one of these is set, matching `kind` — enforced in
    # RecurringTransactionSerializer.validate(), not a DB CHECK constraint
    # (that rule is only ever exercised through this one serializer).
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, null=True, blank=True, related_name="recurring_transactions"
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, null=True, blank=True, related_name="recurring_transactions"
    )

    line_item_presets = models.JSONField(
        default=list, blank=True, help_text='[{"description": ..., "quantity": ..., "unit_price": ...}, ...]'
    )
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)
    notes = models.TextField(blank=True, default="")
    due_in_days = models.PositiveIntegerField(
        default=30, help_text="Generated document's due_date = occurrence date + this many days."
    )

    recurrence_rule = models.CharField(max_length=16, choices=Recurrence.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Null means ongoing, no end date.")
    is_active = models.BooleanField(
        default=True, help_text="Pausing this stops future expansion without deleting history."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["business", "start_date"]

    def __str__(self):
        return f"{self.get_kind_display()} recurring ({self.recurrence_rule}) @ {self.business}"


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        CANCELLED = "cancelled", "Cancelled"
        # Set only by services.record_refund, when a Refund (this app's
        # answer to the "refunds deferred to part 2, never built" gap on
        # Payment) brings net_paid_total to zero or below. A *partial*
        # refund instead reverts a PAID invoice back to SENT (there's
        # still an outstanding balance, same as before it was ever fully
        # paid) — see record_refund's docstring for the full reasoning.
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="invoices")
    # PROTECT, not SET_NULL/CASCADE — a Customer with invoices is a
    # financial record; deleting them out from under an invoice would
    # destroy audit trail. Deactivate the Customer (is_active=False)
    # instead of deleting if they have invoice history.
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")
    revenue_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    recurring_transaction = models.ForeignKey(
        RecurringTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="generated_invoices"
    )
    # Which occurrence of recurring_transaction this row represents —
    # the idempotency key for finance/recurring.py:expand_recurring_transaction,
    # mirroring EmployeeShift's (recurring_schedule, start_at) constraint.
    recurring_occurrence_date = models.DateField(null=True, blank=True)

    invoice_number = models.CharField(max_length=32, editable=False)

    # Tax/discount inputs — see finance/tax.py for the ported calculation.
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)

    # Server-computed (finance/services.py:_recalculate_totals) from line
    # items + the inputs above — never client-written. Stored, not just
    # derived on read, so a saved invoice's breakdown is stable even if
    # tax_type's rate table changes in some future code revision.
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    due_date = models.DateField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["business", "invoice_number"], name="unique_invoice_number_per_business"),
            models.UniqueConstraint(
                fields=["recurring_transaction", "recurring_occurrence_date"],
                condition=models.Q(recurring_transaction__isnull=False),
                name="unique_invoice_per_recurring_transaction_occurrence",
            ),
        ]

    @property
    def paid_total(self) -> Decimal:
        return self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    @property
    def refunded_total(self) -> Decimal:
        return Refund.objects.filter(payment__invoice_id=self.id).aggregate(total=models.Sum("amount"))[
            "total"
        ] or Decimal("0")

    @property
    def net_paid_total(self) -> Decimal:
        """What's actually still paid, after refunds. The basis for record_refund's status recalculation."""
        return self.paid_total - self.refunded_total

    def __str__(self):
        return f"{self.invoice_number} ({self.status})"


class InvoiceLineItem(models.Model):
    """No tax_rate field — see finance/tax.py module docstring for why."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["invoice", "sort_order"]

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.description} x{self.quantity}"


class Payment(models.Model):
    """
    Append-only, like inventory.InventoryTransaction — a financial ledger
    entry shouldn't be editable after creation. Refunds/reversals are a
    separate, equally append-only Refund row (see below) — never an edit
    to this one.
    """

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        STRIPE = "stripe", "Stripe"
        CHECK = "check", "Check"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="payments")
    invoice = models.ForeignKey(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    deposit_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=16, choices=Method.choices)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="recorded_payments"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("Payment is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Payment is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"{self.amount} via {self.method} ({self.created_at:%Y-%m-%d})"


class Refund(models.Model):
    """
    The actual fix for the gap Payment's docstring used to describe as
    "deferred to part 2" — never built when part 2 happened (confirmed by
    grep: zero `refund` references anywhere in this app before this).
    A Refund is its own append-only ledger entry, same immutability
    principle as Payment/PointsTransaction/GiftCardTransaction/
    InventoryTransaction — never an edit or delete of the original
    Payment, which correctly continues to reject both.

    PROTECT on `payment`, not CASCADE — same reasoning applied to every
    other ledger-references-ledger relationship audited this session
    (loyalty.PointsTransaction.account, loyalty.GiftCardTransaction.gift_card):
    a Payment can never actually be deleted today (it raises TypeError),
    so this is defense in depth, not a live concern.

    If a Payment/Invoice ever gets coupled to loyalty points in the
    future (no such coupling exists today — confirmed: only
    loyalty.Order triggers points, see the cross-domain audit), a refund
    should be positioned to reverse those too at the point it's recorded.
    Not built now since there's nothing to reverse yet.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="refunds")
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="recorded_refunds"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("Refund is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Refund is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"Refund of {self.amount} on {self.payment}"


class Bill(models.Model):
    """Bills owed to a Vendor — mirrors Invoice's shape exactly (see finance/tax.py for the shared calculation)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        RECEIVED = "received", "Received"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="bills")
    # PROTECT, not SET_NULL/CASCADE — same reasoning as Invoice.customer:
    # a Vendor with bill history is a financial record, not something to
    # silently lose the link to. Deactivate the Vendor instead of deleting.
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="bills")
    expense_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="bills"
    )
    recurring_transaction = models.ForeignKey(
        RecurringTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="generated_bills"
    )
    recurring_occurrence_date = models.DateField(null=True, blank=True)

    bill_number = models.CharField(max_length=32, editable=False)

    discount_type = models.CharField(max_length=16, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    due_date = models.DateField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["business", "bill_number"], name="unique_bill_number_per_business"),
            models.UniqueConstraint(
                fields=["recurring_transaction", "recurring_occurrence_date"],
                condition=models.Q(recurring_transaction__isnull=False),
                name="unique_bill_per_recurring_transaction_occurrence",
            ),
        ]

    @property
    def paid_total(self) -> Decimal:
        return self.bill_payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    def __str__(self):
        return f"{self.bill_number} ({self.status})"


class BillLineItem(models.Model):
    """No tax_rate field — same reasoning as InvoiceLineItem (finance/tax.py)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["bill", "sort_order"]

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.description} x{self.quantity}"


class BillPayment(models.Model):
    """Append-only — mirrors Payment exactly (see its docstring); same reuse instruction applies."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CHECK = "check", "Check"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="bill_payments")
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="bill_payments")
    payment_account = models.ForeignKey(
        ChartOfAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_payments",
        help_text="Which account the money was paid from.",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=16, choices=Method.choices)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="recorded_bill_payments"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("BillPayment is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("BillPayment is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"{self.amount} via {self.method} ({self.created_at:%Y-%m-%d})"


class BankTransaction(models.Model):
    """
    Manual entry only for now — see README "Finance domain" -> "Bank
    transactions". `source` and `external_transaction_id` exist
    specifically so a future bank-feed/Plaid-style import could populate
    this model and de-duplicate against re-imports, without a schema
    change when that's built — no actual integration exists yet; every
    row created through this session's API has `source=manual` and an
    empty `external_transaction_id`.

    `reconciled_object` (GenericForeignKey) can point at any one of
    Invoice/Payment/Bill/BillPayment — the four things a bank line could
    plausibly match against. A GenericForeignKey is used here specifically
    because there are 4 distinct possible target *types* for one
    relationship; a bare GFK doesn't restrict which models can be
    referenced on its own, so `services.reconcile_bank_transaction` (the
    only thing that sets it) is what enforces the allow-list.
    """

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        IMPORTED = "imported", "Imported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="bank_transactions")
    date = models.DateField()
    description = models.CharField(max_length=500, blank=True, default="")
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Positive for a credit, negative for a debit."
    )
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    external_transaction_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Set by a future bank-feed import; always blank for manual entry.",
    )

    is_reconciled = models.BooleanField(default=False)
    reconciled_content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reconciled_object_id = models.UUIDField(null=True, blank=True)
    reconciled_object = GenericForeignKey("reconciled_content_type", "reconciled_object_id")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "external_transaction_id"],
                condition=~models.Q(external_transaction_id=""),
                name="unique_bank_transaction_external_id_per_business",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.description} {self.amount}"


class Estimate(models.Model):
    """Pre-sale version of an Invoice — same tax/discount shape, convertible via services.convert_estimate_to_invoice."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CONVERTED = "converted", "Converted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="estimates")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="estimates")
    estimate_number = models.CharField(max_length=32, editable=False)

    discount_type = models.CharField(max_length=16, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    expires_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    converted_invoice = models.OneToOneField(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_estimate"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "estimate_number"], name="unique_estimate_number_per_business"
            ),
        ]

    def __str__(self):
        return f"{self.estimate_number} ({self.status})"


class EstimateLineItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["estimate", "sort_order"]

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.description} x{self.quantity}"


class InvoiceTemplate(models.Model):
    """
    Reusable presets — `line_item_presets` is plain JSON (not validated
    as strictly as a real InvoiceLineItem) because it's only ever a
    pre-fill default; applying a template copies its presets into real,
    validated InvoiceLineItem rows on the invoice being created, which is
    where real validation happens.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="invoice_templates")
    name = models.CharField(max_length=255)
    line_item_presets = models.JSONField(
        default=list, blank=True, help_text='[{"description": ..., "quantity": ..., "unit_price": ...}, ...]'
    )
    default_tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)
    default_notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_invoice_template_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return f"{self.name} @ {self.business}"


class StripeWebhookEvent(models.Model):
    """
    Idempotency record — see webhooks.py for why event_id (Stripe's own
    "evt_..." id) is the primary key rather than a separate unique field,
    and why this row's insert shares one transaction with the handler's
    side effects.
    """

    event_id = models.CharField(max_length=255, primary_key=True, editable=False)
    event_type = models.CharField(max_length=255)
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type} ({self.event_id})"
