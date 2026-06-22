"""
Loyalty & Gift Cards domain — including Orders, which the Phase 1 audit
found tightly coupled to loyalty (creating an order auto-awards points
client-side today, non-atomically). The single strongest case in the
whole system for backend-enforced transactions: every balance mutation
here goes through transaction.atomic() + select_for_update(), the same
pattern already proven in inventory.services.adjust_stock,
finance.services.record_payment/record_bill_payment, and
reservations.services' table-locking — never a client-trusted balance.

  orders                (old) -> Order
  order_line_items      (old) -> OrderLineItem
  loyalty_programs      (old) -> LoyaltyProgram
  customer_loyalty_accounts (old) -> CustomerLoyaltyAccount
  points_transactions   (old) -> PointsTransaction
  gift_cards            (old) -> GiftCard
  gift_card_transactions(old) -> GiftCardTransaction

Tax calculation on Order reuses finance.tax.calculate_totals directly
(same TaxType/DiscountType, same Decimal precision) rather than a second
implementation — an order total is computed exactly like an
Invoice/Bill total. OrderLineItem has no tax_rate field for the same
reason InvoiceLineItem/BillLineItem don't (see finance/tax.py).

The actual fixes (Phase 1 audit findings):

  1. Points accrual/redemption was entirely client-side, non-atomic
     read-modify-write. `loyalty/services.py`: `award_points`/`redeem_points`
     lock the `CustomerLoyaltyAccount` row; `redeem_points` rejects
     outright (never clamps) an amount that would take `available_points`
     negative.

  2. Gift card balance reloads/redemptions had the same risk.
     `reload_gift_card`/`redeem_gift_card` mirror award_points/redeem_points
     exactly, locking `GiftCard`; `redeem_gift_card` additionally rejects
     an expired or inactive card.

  3. `current_tier` existed as a field but was never actually computed
     anywhere. It's now derived from `lifetime_points` (monotonically
     increasing — redemptions never reduce it) against `LoyaltyProgram`'s
     threshold fields, recalculated inside the same atomic block as every
     `lifetime_points` change. See services.py:`_recalculate_tier` and
     README "Loyalty domain" for the exact rule and why lifetime points
     rather than lifetime spend.

  4. No expiration enforcement existed on points or gift card balances.
     `LoyaltyProgram.points_expire_after_days` is optional
     (null = never expires, the default — there's no expiration window in
     the original data model to port, so a mandatory policy isn't
     invented here). When set, `PointsTransaction.expires_at` is stamped
     on each earn transaction at creation, and a daily Celery Beat task
     (loyalty/tasks.py) expires it — see PointsTransaction's docstring for
     why this is a deliberate per-grant simplification, not full
     multi-grant FIFO lot tracking. `GiftCard.expires_at` is enforced
     directly in `redeem_gift_card`.

  5. QR code generation depended on an external service (QRServer.com).
     Replaced with the `qrcode` library, generated on demand
     (loyalty/qr.py) — see that module's docstring for why this isn't
     stored as a Document.

  6. Orders auto-awarded points non-atomically on creation.
     `create_order_and_award_points` (services.py) creates the Order and
     awards the resulting points in one `transaction.atomic()` block — if
     either step fails, neither applies. Proven directly
     (loyalty.tests/test_orders — a forced failure between order creation
     and the points award rolls back the order too, not just the points).
"""

import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import Business, BusinessMembership
from customers.models import Customer
from finance.tax import DiscountType, TaxType


class Order(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="orders")
    # PROTECT — same reasoning as Invoice.customer/Bill.vendor: an order is
    # a financial record, not something to lose the link to.
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    # An Order can exist on its own (POS-style instant sale) OR later be
    # converted into a billed Invoice via services.convert_order_to_invoice
    # — see that function's docstring for the deliberate choice that Order
    # and Invoice become fully independent once linked (cancelling the
    # Order only ever reverses its own points; it never touches an
    # already-issued Invoice's status, which has its own state machine and
    # its own rules — e.g. a paid invoice can't be cancelled either).
    # SET_NULL, not CASCADE/PROTECT: deleting the Invoice shouldn't delete
    # or block deleting the real sale record (the Order) that generated it.
    invoice = models.ForeignKey(
        "finance.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_orders"
    )

    # Tax/discount inputs — see finance/tax.py for the shared calculation.
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.ZERO)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COMPLETED)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order {self.id} ({self.status})"


class OrderLineItem(models.Model):
    """No tax_rate field — same reasoning as InvoiceLineItem/BillLineItem (finance/tax.py)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "sort_order"]

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.description} x{self.quantity}"


class LoyaltyProgram(models.Model):
    """
    Tier thresholds are explicit integer fields, not a JSON blob — there
    are exactly 3 (silver/gold/platinum; bronze is the implicit baseline,
    lifetime_points >= 0), so a real, typed field per threshold is
    simpler and safer than an unvalidated JSON shape for something this
    small and fixed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="loyalty_programs")
    name = models.CharField(max_length=255)
    points_per_dollar = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.00"), validators=[MinValueValidator(Decimal("0"))]
    )

    silver_threshold = models.PositiveIntegerField(default=500, help_text="Lifetime points required to reach Silver.")
    gold_threshold = models.PositiveIntegerField(default=2000, help_text="Lifetime points required to reach Gold.")
    platinum_threshold = models.PositiveIntegerField(
        default=5000, help_text="Lifetime points required to reach Platinum."
    )

    points_expire_after_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Null (default) means points never expire."
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_loyalty_program_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return f"{self.name} @ {self.business}"


class CustomerLoyaltyAccount(models.Model):
    class Tier(models.TextChoices):
        BRONZE = "bronze", "Bronze"
        SILVER = "silver", "Silver"
        GOLD = "gold", "Gold"
        PLATINUM = "platinum", "Platinum"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT, not CASCADE — same reasoning as Order.customer/Invoice.customer/
    # Bill.vendor: this account's PointsTransaction ledger is append-only
    # history, and a Customer CASCADE here would silently bulk-delete it via
    # SQL (bypassing PointsTransaction.delete()'s own block entirely, since
    # cascading deletes never go through model instance methods). Deactivate
    # the Customer instead of deleting one with loyalty history.
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="loyalty_accounts")
    loyalty_program = models.ForeignKey(LoyaltyProgram, on_delete=models.PROTECT, related_name="accounts")
    available_points = models.PositiveIntegerField(default=0)
    # Never decreases (redemptions only reduce available_points) — the
    # basis for current_tier. See services.py:_recalculate_tier.
    lifetime_points = models.PositiveIntegerField(default=0)
    current_tier = models.CharField(max_length=16, choices=Tier.choices, default=Tier.BRONZE, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "loyalty_program"], name="unique_account_per_customer_program"),
        ]
        ordering = ["customer"]

    @property
    def business(self):
        return self.loyalty_program.business

    def __str__(self):
        return f"{self.customer} @ {self.loyalty_program} ({self.current_tier})"


class PointsTransaction(models.Model):
    """
    Append-only, like inventory.InventoryTransaction. Expiration
    (`expired_transaction`) is itself implemented as a new compensating
    row pointing back at the original earn transaction, rather than
    mutating the original's `is_expired`/similar flag — preserving true
    append-only immutability while still giving the expiration task an
    idempotency key (a unique constraint on `expired_transaction`, same
    insert-and-let-the-constraint-catch-duplicates shape as
    StripeWebhookEvent / the recurring-transaction expansion).

    Deliberate simplification: this expires *a specific grant*, capped at
    `min(original points_change, account.available_points)` — not true
    multi-grant FIFO lot tracking (knowing precisely which surviving
    points came from which grant after partial redemptions). It's
    guaranteed to never take the account negative and never expire more
    than that one grant earned; it does not guarantee strict
    oldest-points-first consumption order across multiple grants. See
    README "Loyalty domain" for the full reasoning — chosen because the
    original system has no expiration policy to port at all, so a
    precisely "correct" multi-grant ledger would be inventing complexity
    beyond what's asked for, not faithfully reproducing anything.
    """

    class Reason(models.TextChoices):
        ORDER = "order", "Order"
        REDEMPTION = "redemption", "Redemption"
        MANUAL = "manual", "Manual adjustment"
        EXPIRATION = "expiration", "Expiration"
        REVERSAL = "reversal", "Reversal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT, not CASCADE — deleting the account directly (as opposed to
    # it ever happening at all, now that customer/loyalty_program above are
    # also PROTECT) must not be able to silently bulk-delete this ledger via
    # SQL, the same way deleting an Invoice can't take its Payments with it.
    # An account with zero transactions (never awarded anything) is still
    # freely deletable; the first PointsTransaction makes it permanent.
    account = models.ForeignKey(CustomerLoyaltyAccount, on_delete=models.PROTECT, related_name="points_transactions")
    points_change = models.IntegerField()
    reason = models.CharField(max_length=16, choices=Reason.choices)
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="points_transactions"
    )
    notes = models.CharField(max_length=255, blank=True, default="")
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="Set only on earn transactions, when the program defines an expiration window."
    )
    expired_transaction = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expiration_compensations",
        help_text="Set only on a compensating EXPIRATION transaction — points at the earn transaction it expires.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["expired_transaction"],
                condition=models.Q(expired_transaction__isnull=False),
                name="unique_expiration_per_transaction",
            ),
        ]

    @property
    def business(self):
        return self.account.loyalty_program.business

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("PointsTransaction is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PointsTransaction is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"{self.account} {self.points_change:+} ({self.reason})"


class GiftCard(models.Model):
    """`code` is generated server-side (secrets.token_urlsafe) — same standard as marketing.TrackingScript.script_key, never client-chosen, never sequential."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="gift_cards")
    code = models.CharField(max_length=64, unique=True, editable=False)
    initial_balance = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    current_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    recipient_name = models.CharField(max_length=255, blank=True, default="")
    recipient_email = models.EmailField(blank=True, default="")
    purchaser_customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchased_gift_cards"
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def __str__(self):
        return f"Gift card {self.code} ({self.current_balance})"


class GiftCardTransaction(models.Model):
    """Append-only, mirrors PointsTransaction/InventoryTransaction exactly."""

    class Reason(models.TextChoices):
        INITIAL = "initial", "Initial balance"
        RELOAD = "reload", "Reload"
        REDEMPTION = "redemption", "Redemption"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT, not CASCADE — same reasoning as PointsTransaction.account.
    # create_gift_card always creates an INITIAL transaction alongside the
    # card, so in practice every gift card is permanent (only
    # deactivatable via is_active=False) from the moment it's issued —
    # which is correct: an issued gift card represents real money received.
    gift_card = models.ForeignKey(GiftCard, on_delete=models.PROTECT, related_name="transactions")
    amount_change = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=16, choices=Reason.choices)
    notes = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="gift_card_transactions"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def business(self):
        return self.gift_card.business

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("GiftCardTransaction is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("GiftCardTransaction is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"{self.gift_card} {self.amount_change:+} ({self.reason})"
