"""
Loyalty/order/gift-card service layer.

Every balance mutation goes through here — never a generic
serializer.save() field write, and never a client-trusted "new balance."
See models.py module docstring for the Phase 1 audit findings this
addresses (the single strongest case for backend-enforced transactions in
the whole system).
"""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.email import send_email
from finance.tax import TaxLineItem, calculate_totals

from .models import (
    CustomerLoyaltyAccount,
    GiftCard,
    GiftCardTransaction,
    LoyaltyProgram,
    Order,
    OrderLineItem,
    PointsTransaction,
)


class LoyaltyError(Exception):
    """Base for all domain errors raised by this module. Views translate these to 400s."""


class InvalidOrderStateError(LoyaltyError):
    pass


class InsufficientPointsError(LoyaltyError):
    def __init__(self, amount, available_points):
        self.amount = amount
        self.available_points = available_points
        super().__init__(f"Cannot redeem {amount} points; only {available_points} available.")


class InsufficientGiftCardBalanceError(LoyaltyError):
    def __init__(self, amount, current_balance):
        self.amount = amount
        self.current_balance = current_balance
        super().__init__(f"Cannot redeem {amount}; gift card balance is only {current_balance}.")


class InactiveGiftCardError(LoyaltyError):
    pass


class ExpiredGiftCardError(LoyaltyError):
    pass


# --- Tax / line items (reused from finance.tax, not reimplemented) -------------


def _line_items_for_tax(line_items_data) -> list:
    return [TaxLineItem(quantity=Decimal(str(d["quantity"])), rate=Decimal(str(d["unit_price"]))) for d in line_items_data]


def _apply_order_totals(order: Order, line_items_data) -> None:
    result = calculate_totals(_line_items_for_tax(line_items_data), order.discount_type, order.discount_value, order.tax_type)
    order.subtotal = result.subtotal
    order.discount_amount = result.discount
    order.taxable_amount = result.taxable_amount
    order.tax_amount = result.tax
    order.total = result.total


# --- Tier calculation ------------------------------------------------------------


def _tier_for_lifetime_points(lifetime_points: int, program: LoyaltyProgram) -> str:
    """See README "Loyalty domain" for the exact rule: lifetime_points (never decreases) against fixed thresholds."""
    if lifetime_points >= program.platinum_threshold:
        return CustomerLoyaltyAccount.Tier.PLATINUM
    if lifetime_points >= program.gold_threshold:
        return CustomerLoyaltyAccount.Tier.GOLD
    if lifetime_points >= program.silver_threshold:
        return CustomerLoyaltyAccount.Tier.SILVER
    return CustomerLoyaltyAccount.Tier.BRONZE


def _recalculate_tier(account: CustomerLoyaltyAccount) -> None:
    """Mutates account.current_tier in place — caller is responsible for saving."""
    account.current_tier = _tier_for_lifetime_points(account.lifetime_points, account.loyalty_program)


# --- Points ------------------------------------------------------------------------


def award_points(account: CustomerLoyaltyAccount, amount: int, reason, order: Order = None, notes="") -> PointsTransaction:
    """
    The only way available_points/lifetime_points increase. Locks the
    account row; recalculates current_tier in the same atomic block,
    since lifetime_points just changed. Stamps expires_at on the new
    transaction if the program defines an expiration window — see
    PointsTransaction's docstring for the expiration model.
    """
    if amount <= 0:
        raise LoyaltyError("award_points amount must be positive.")

    with transaction.atomic():
        locked = CustomerLoyaltyAccount.objects.select_for_update().get(pk=account.pk)
        locked.available_points += amount
        locked.lifetime_points += amount
        _recalculate_tier(locked)
        locked.save(update_fields=["available_points", "lifetime_points", "current_tier", "updated_at"])

        program = locked.loyalty_program
        expires_at = (
            timezone.now() + timedelta(days=program.points_expire_after_days)
            if program.points_expire_after_days
            else None
        )
        return PointsTransaction.objects.create(
            account=locked, points_change=amount, reason=reason, order=order, notes=notes, expires_at=expires_at
        )


def redeem_points(account: CustomerLoyaltyAccount, amount: int, notes="") -> PointsTransaction:
    """
    Rejects outright (never clamps) an amount that would take
    available_points negative. lifetime_points — and therefore
    current_tier — is unchanged by a redemption (tier never drops just
    because points were spent); _recalculate_tier is still called for
    consistency, but it's a no-op here since lifetime_points didn't move.
    """
    if amount <= 0:
        raise LoyaltyError("redeem_points amount must be positive.")

    with transaction.atomic():
        locked = CustomerLoyaltyAccount.objects.select_for_update().get(pk=account.pk)
        if amount > locked.available_points:
            raise InsufficientPointsError(amount, locked.available_points)

        locked.available_points -= amount
        _recalculate_tier(locked)
        locked.save(update_fields=["available_points", "current_tier", "updated_at"])

        return PointsTransaction.objects.create(
            account=locked, points_change=-amount, reason=PointsTransaction.Reason.REDEMPTION, notes=notes
        )


def expire_due_points() -> int:
    """
    Daily Celery Beat task entry point (loyalty/tasks.py). For every earn
    transaction past its expires_at with no compensating expiration row
    yet, creates one — see PointsTransaction's docstring for the
    per-grant-with-clamping model this implements. Each earn transaction
    is processed exactly once, ever: the compensating row (even one with
    points_change=0, if the points were already fully redeemed by the
    time this runs) is what marks it done, backed by the
    unique_expiration_per_transaction constraint as the concurrency-safe
    idempotency check — same insert-and-let-the-constraint-catch-duplicates
    shape as StripeWebhookEvent.
    """
    due = PointsTransaction.objects.filter(
        expires_at__lt=timezone.now(), expires_at__isnull=False, points_change__gt=0, expiration_compensations__isnull=True
    )

    expired_count = 0
    for earn_transaction in due:
        try:
            with transaction.atomic():
                account = CustomerLoyaltyAccount.objects.select_for_update().get(pk=earn_transaction.account_id)
                amount_to_expire = min(earn_transaction.points_change, account.available_points)
                if amount_to_expire > 0:
                    account.available_points -= amount_to_expire
                    _recalculate_tier(account)
                    account.save(update_fields=["available_points", "current_tier", "updated_at"])

                PointsTransaction.objects.create(
                    account=account,
                    points_change=-amount_to_expire,
                    reason=PointsTransaction.Reason.EXPIRATION,
                    expired_transaction=earn_transaction,
                )
        except IntegrityError:
            # A concurrent run already expired this one between our
            # query above and our insert — nothing more to do.
            continue
        expired_count += 1

    return expired_count


# --- Orders ------------------------------------------------------------------------


def create_order_and_award_points(
    business,
    customer,
    line_items_data,
    tax_type,
    discount_type="none",
    discount_value=Decimal("0"),
    notes="",
    loyalty_program=None,
) -> Order:
    """
    The actual fix for Phase 1 audit finding #6: order creation and the
    points it earns happen in one transaction.atomic() block. If anything
    after the order is created raises (including inside award_points),
    the whole block — order, line items, and any points — rolls back
    together. Never a half-applied state.

    If `loyalty_program` isn't given, the business's first active
    LoyaltyProgram is used (auto-enrolling the customer into it via
    get_or_create if they have no account yet). If the business has no
    active program at all, the order is still created — earning points
    is contingent on a program existing, not a precondition for selling
    something.
    """
    with transaction.atomic():
        order = Order(
            business=business,
            customer=customer,
            tax_type=tax_type,
            discount_type=discount_type,
            discount_value=discount_value,
            notes=notes,
        )
        _apply_order_totals(order, line_items_data)
        order.save()
        OrderLineItem.objects.bulk_create(
            [
                OrderLineItem(
                    order=order,
                    description=d["description"],
                    quantity=d["quantity"],
                    unit_price=d["unit_price"],
                    sort_order=i,
                )
                for i, d in enumerate(line_items_data)
            ]
        )

        program = loyalty_program or LoyaltyProgram.objects.filter(business=business, is_active=True).first()
        if program is not None and order.total > 0:
            account, _created = CustomerLoyaltyAccount.objects.get_or_create(
                customer=customer, loyalty_program=program
            )
            points_to_award = int(order.total * program.points_per_dollar)
            if points_to_award > 0:
                award_points(account, points_to_award, PointsTransaction.Reason.ORDER, order=order)

        return order


def cancel_order(order: Order) -> Order:
    """Reverses any points this order awarded, clamped to the account's current available_points (some may already be spent)."""
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        if locked_order.status == Order.Status.CANCELLED:
            raise InvalidOrderStateError("Order is already cancelled.")

        earn_transaction = locked_order.points_transactions.filter(reason=PointsTransaction.Reason.ORDER).first()
        if earn_transaction is not None:
            account = CustomerLoyaltyAccount.objects.select_for_update().get(pk=earn_transaction.account_id)
            reversal_amount = min(earn_transaction.points_change, account.available_points)
            if reversal_amount > 0:
                account.available_points -= reversal_amount
                _recalculate_tier(account)
                account.save(update_fields=["available_points", "current_tier", "updated_at"])
                PointsTransaction.objects.create(
                    account=account,
                    points_change=-reversal_amount,
                    reason=PointsTransaction.Reason.REVERSAL,
                    order=locked_order,
                )

        locked_order.status = Order.Status.CANCELLED
        locked_order.save(update_fields=["status", "updated_at"])
        return locked_order


# --- Gift cards --------------------------------------------------------------------


def generate_gift_card_code() -> str:
    """Real random token (not sequential/guessable) — same standard as marketing.services.generate_script_key."""
    return secrets.token_urlsafe(32)


def create_gift_card(
    business, initial_balance, recipient_name="", recipient_email="", expires_at=None, purchaser_customer=None
) -> GiftCard:
    with transaction.atomic():
        card = GiftCard.objects.create(
            business=business,
            code=generate_gift_card_code(),
            initial_balance=initial_balance,
            current_balance=initial_balance,
            expires_at=expires_at,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            purchaser_customer=purchaser_customer,
        )
        GiftCardTransaction.objects.create(
            gift_card=card, amount_change=initial_balance, reason=GiftCardTransaction.Reason.INITIAL
        )
        return card


def reload_gift_card(card: GiftCard, amount, membership=None, notes="") -> GiftCardTransaction:
    if amount <= 0:
        raise LoyaltyError("reload_gift_card amount must be positive.")

    with transaction.atomic():
        locked = GiftCard.objects.select_for_update().get(pk=card.pk)
        locked.current_balance += amount
        locked.save(update_fields=["current_balance", "updated_at"])
        return GiftCardTransaction.objects.create(
            gift_card=locked,
            amount_change=amount,
            reason=GiftCardTransaction.Reason.RELOAD,
            notes=notes,
            created_by=membership,
        )


def redeem_gift_card(card: GiftCard, amount, membership=None, notes="") -> GiftCardTransaction:
    """Rejects outright if the card is expired/inactive, or if the amount would take current_balance negative."""
    if amount <= 0:
        raise LoyaltyError("redeem_gift_card amount must be positive.")

    with transaction.atomic():
        locked = GiftCard.objects.select_for_update().get(pk=card.pk)
        if not locked.is_active:
            raise InactiveGiftCardError("Gift card is not active.")
        if locked.is_expired:
            raise ExpiredGiftCardError("Gift card has expired.")
        if amount > locked.current_balance:
            raise InsufficientGiftCardBalanceError(amount, locked.current_balance)

        locked.current_balance -= amount
        locked.save(update_fields=["current_balance", "updated_at"])
        return GiftCardTransaction.objects.create(
            gift_card=locked,
            amount_change=-amount,
            reason=GiftCardTransaction.Reason.REDEMPTION,
            notes=notes,
            created_by=membership,
        )


def send_gift_card_email(card: GiftCard) -> GiftCard:
    """First caller of core.email.send_email — see that module's docstring."""
    if not card.recipient_email:
        raise LoyaltyError("Gift card has no recipient_email to send to.")

    html_body = (
        f"<p>You've received a gift card worth {card.current_balance}!</p>"
        f"<p>Your gift card code: <strong>{card.code}</strong></p>"
    )
    send_email(to=card.recipient_email, subject="You've received a gift card!", html_body=html_body)
    card.sent_at = timezone.now()
    card.save(update_fields=["sent_at", "updated_at"])
    return card
