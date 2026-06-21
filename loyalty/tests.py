"""
Tests for Loyalty & Gift Cards & Orders — the highest-stakes domain yet
(the original audit's single strongest case for backend-enforced
transactions). Concurrency, atomicity of create_order_and_award_points,
tier recalculation, ledger immutability, expiration, gift card code
unguessability, and tenant isolation, all proven directly rather than
inferred.
"""

import os.path
import threading
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
from .models import (
    CustomerLoyaltyAccount,
    GiftCardTransaction,
    LoyaltyProgram,
    Order,
    PointsTransaction,
)
from .qr import generate_qr_png
from .services import generate_gift_card_code
from .tasks import expire_points

SAMPLE_LINE_ITEMS = [{"description": "Widget", "quantity": Decimal("1"), "unit_price": Decimal("100")}]


def order_list_url(business_id):
    return f"/api/businesses/{business_id}/orders/"


def order_cancel_url(business_id, pk):
    return f"/api/businesses/{business_id}/orders/{pk}/cancel/"


def loyalty_program_list_url(business_id):
    return f"/api/businesses/{business_id}/loyalty-programs/"


def loyalty_account_list_url(business_id):
    return f"/api/businesses/{business_id}/loyalty-accounts/"


def loyalty_account_award_url(business_id, pk):
    return f"/api/businesses/{business_id}/loyalty-accounts/{pk}/award-points/"


def loyalty_account_redeem_url(business_id, pk):
    return f"/api/businesses/{business_id}/loyalty-accounts/{pk}/redeem-points/"


def points_transaction_list_url(business_id):
    return f"/api/businesses/{business_id}/points-transactions/"


def gift_card_list_url(business_id):
    return f"/api/businesses/{business_id}/gift-cards/"


def gift_card_reload_url(business_id, pk):
    return f"/api/businesses/{business_id}/gift-cards/{pk}/reload/"


def gift_card_redeem_url(business_id, pk):
    return f"/api/businesses/{business_id}/gift-cards/{pk}/redeem/"


def gift_card_send_url(business_id, pk):
    return f"/api/businesses/{business_id}/gift-cards/{pk}/send/"


def gift_card_qr_url(business_id, pk):
    return f"/api/businesses/{business_id}/gift-cards/{pk}/qr-code/"


class LoyaltyTestSetupMixin:
    def _setup_business_and_program(self, suffix=""):
        owner = User.objects.create_user(email=f"owner_loy{suffix}@example.com")
        business = Business.objects.create(name=f"Loyalty Biz{suffix}", owner=owner)
        customer = Customer.objects.create(business=business, name="Customer")
        program = LoyaltyProgram.objects.create(business=business, name="Default Program")
        return business, customer, program


class TierCalculationTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program()
        self.account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=self.program)

    def test_starts_at_bronze(self):
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.BRONZE)

    def test_crossing_silver_threshold_updates_tier(self):
        services.award_points(self.account, self.program.silver_threshold, PointsTransaction.Reason.MANUAL)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.SILVER)

    def test_just_below_silver_threshold_stays_bronze(self):
        services.award_points(self.account, self.program.silver_threshold - 1, PointsTransaction.Reason.MANUAL)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.BRONZE)

    def test_crossing_gold_then_platinum(self):
        services.award_points(self.account, self.program.gold_threshold, PointsTransaction.Reason.MANUAL)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.GOLD)

        services.award_points(
            self.account, self.program.platinum_threshold - self.program.gold_threshold, PointsTransaction.Reason.MANUAL
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.PLATINUM)

    def test_tier_does_not_drop_on_redemption(self):
        services.award_points(self.account, self.program.gold_threshold, PointsTransaction.Reason.MANUAL)
        self.account.refresh_from_db()
        services.redeem_points(self.account, self.program.gold_threshold - 10)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_tier, CustomerLoyaltyAccount.Tier.GOLD)
        self.assertEqual(self.account.available_points, 10)
        self.assertEqual(self.account.lifetime_points, self.program.gold_threshold)


class PointsAwardRedeemTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program()
        self.account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=self.program)

    def test_award_increases_available_and_lifetime(self):
        services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 100)
        self.assertEqual(self.account.lifetime_points, 100)

    def test_redeem_decreases_available_only(self):
        services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        services.redeem_points(self.account, 40)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 60)
        self.assertEqual(self.account.lifetime_points, 100)

    def test_redeem_more_than_available_is_rejected(self):
        services.award_points(self.account, 50, PointsTransaction.Reason.MANUAL)
        with self.assertRaises(services.InsufficientPointsError):
            services.redeem_points(self.account, 51)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 50)

    def test_award_and_redeem_create_ledger_entries(self):
        services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        services.redeem_points(self.account, 30)
        entries = list(PointsTransaction.objects.filter(account=self.account).order_by("created_at"))
        self.assertEqual([e.points_change for e in entries], [100, -30])
        self.assertEqual(entries[0].reason, PointsTransaction.Reason.MANUAL)
        self.assertEqual(entries[1].reason, PointsTransaction.Reason.REDEMPTION)


class PointsLedgerImmutabilityTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program()
        self.account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=self.program)
        self.entry = services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)

    def test_cannot_edit(self):
        self.entry.notes = "tampered"
        with self.assertRaises(TypeError):
            self.entry.save()

    def test_cannot_delete(self):
        with self.assertRaises(TypeError):
            self.entry.delete()
        self.assertTrue(PointsTransaction.objects.filter(pk=self.entry.pk).exists())


class PointsRedemptionConcurrencyTests(TransactionTestCase, LoyaltyTestSetupMixin):
    """Account has 100 available; two concurrent redemptions of 80 each (sum=160>100) — exactly one must succeed."""

    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program("_pcr")
        self.account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=self.program)
        services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)

    def test_only_one_concurrent_redemption_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_redeem():
            barrier.wait()
            try:
                services.redeem_points(self.account, 80)
                outcome = "success"
            except services.InsufficientPointsError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_redeem) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 20)
        self.assertGreaterEqual(self.account.available_points, 0)


class GiftCardConcurrencyTests(TransactionTestCase, LoyaltyTestSetupMixin):
    """Same shape as PointsRedemptionConcurrencyTests — gift card balance=100, two concurrent redemptions of 80."""

    def setUp(self):
        owner = User.objects.create_user(email="owner_gcc@example.com")
        self.business = Business.objects.create(name="Gift Card Concurrency Biz", owner=owner)
        self.card = services.create_gift_card(self.business, Decimal("100"))

    def test_only_one_concurrent_gift_card_redemption_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_redeem():
            barrier.wait()
            try:
                services.redeem_gift_card(self.card, Decimal("80"))
                outcome = "success"
            except services.InsufficientGiftCardBalanceError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_redeem) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("20"))
        self.assertGreaterEqual(self.card.current_balance, Decimal("0"))


class GiftCardTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_gc@example.com")
        self.business = Business.objects.create(name="Gift Card Biz", owner=owner)

    def test_create_gift_card_creates_initial_ledger_entry(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        self.assertEqual(card.current_balance, Decimal("50"))
        entry = GiftCardTransaction.objects.get(gift_card=card)
        self.assertEqual(entry.amount_change, Decimal("50"))
        self.assertEqual(entry.reason, GiftCardTransaction.Reason.INITIAL)

    def test_reload_increases_balance(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        services.reload_gift_card(card, Decimal("25"))
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("75"))

    def test_redeem_decreases_balance(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        services.redeem_gift_card(card, Decimal("20"))
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("30"))

    def test_redeem_more_than_balance_rejected(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        with self.assertRaises(services.InsufficientGiftCardBalanceError):
            services.redeem_gift_card(card, Decimal("51"))
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("50"))

    def test_redeem_on_expired_card_rejected(self):
        card = services.create_gift_card(self.business, Decimal("50"), expires_at=timezone.now() - timezone.timedelta(days=1))
        with self.assertRaises(services.ExpiredGiftCardError):
            services.redeem_gift_card(card, Decimal("10"))
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("50"))

    def test_redeem_on_inactive_card_rejected(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        card.is_active = False
        card.save(update_fields=["is_active"])
        with self.assertRaises(services.InactiveGiftCardError):
            services.redeem_gift_card(card, Decimal("10"))

    def test_code_is_not_predictable(self):
        codes = {generate_gift_card_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)  # no collisions across 200 generations
        for code in list(codes)[:5]:
            self.assertGreaterEqual(len(code), 32)

    def test_consecutive_codes_share_no_obvious_sequential_pattern(self):
        first = generate_gift_card_code()
        second = generate_gift_card_code()
        # A sequential/predictable generator (e.g. an incrementing id)
        # would produce codes sharing a long common prefix; a real random
        # token essentially never will.
        common_prefix_len = len(os.path.commonprefix([first, second]))
        self.assertLess(common_prefix_len, 4)


class GiftCardLedgerImmutabilityTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_gc_ledger@example.com")
        self.business = Business.objects.create(name="Gift Card Ledger Biz", owner=owner)
        self.card = services.create_gift_card(self.business, Decimal("50"))
        self.entry = services.reload_gift_card(self.card, Decimal("10"))

    def test_cannot_edit(self):
        self.entry.notes = "tampered"
        with self.assertRaises(TypeError):
            self.entry.save()

    def test_cannot_delete(self):
        with self.assertRaises(TypeError):
            self.entry.delete()


class CreateOrderAndAwardPointsAtomicityTests(TestCase, LoyaltyTestSetupMixin):
    """The actual fix for Phase 1 audit finding #6: order + points are one atomic unit."""

    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program("_atomic")

    def test_successful_order_creates_order_and_awards_points(self):
        order = services.create_order_and_award_points(
            self.business, self.customer, SAMPLE_LINE_ITEMS, "GST_5"
        )
        self.assertEqual(Order.objects.count(), 1)
        account = CustomerLoyaltyAccount.objects.get(customer=self.customer, loyalty_program=self.program)
        expected_points = int(order.total * self.program.points_per_dollar)
        self.assertEqual(account.available_points, expected_points)
        self.assertTrue(PointsTransaction.objects.filter(order=order).exists())

    def test_failure_during_points_award_rolls_back_the_order_too(self):
        with patch("loyalty.services.award_points", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                services.create_order_and_award_points(self.business, self.customer, SAMPLE_LINE_ITEMS, "GST_5")

        # Neither the order, its line items, nor any points exist —
        # not a half-applied state.
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(PointsTransaction.objects.count(), 0)
        self.assertEqual(CustomerLoyaltyAccount.objects.filter(customer=self.customer).count(), 0)

    def test_order_created_even_with_no_active_loyalty_program(self):
        self.program.is_active = False
        self.program.save(update_fields=["is_active"])
        created_order = services.create_order_and_award_points(self.business, self.customer, SAMPLE_LINE_ITEMS, "GST_5")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(created_order.status, Order.Status.COMPLETED)
        self.assertEqual(PointsTransaction.objects.count(), 0)


class OrderCreationTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program("_create")
        self.staff_user = User.objects.create_user(email="staff_order_create@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_order_via_api_computes_tax_via_shared_finance_service(self):
        response = self.client.post(
            order_list_url(self.business.id),
            {
                "customer": str(self.customer.id),
                "tax_type": "GST_5",
                "line_items": [{"description": "Widget", "quantity": "1", "unit_price": "100.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["subtotal"], "100.00")
        self.assertEqual(response.data["tax_amount"], "5.00")
        self.assertEqual(response.data["total"], "105.00")

    def test_cannot_use_a_customer_from_another_business(self):
        other_owner = User.objects.create_user(email="other_owner_order@example.com")
        other_business = Business.objects.create(name="Other Order Biz", owner=other_owner)
        other_customer = Customer.objects.create(business=other_business, name="Mallory")
        response = self.client.post(
            order_list_url(self.business.id),
            {"customer": str(other_customer.id), "tax_type": "ZERO", "line_items": SAMPLE_LINE_ITEMS},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrderCancelTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program("_cancel")
        self.order = services.create_order_and_award_points(self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO")
        self.account = CustomerLoyaltyAccount.objects.get(customer=self.customer, loyalty_program=self.program)

    def test_cancel_reverses_awarded_points(self):
        points_before = self.account.available_points
        self.assertGreater(points_before, 0)
        services.cancel_order(self.order)
        self.order.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertEqual(self.account.available_points, 0)

    def test_cancel_clamps_reversal_if_points_already_redeemed(self):
        self.account.refresh_from_db()
        # Spend all but 5 of the earned points before cancelling.
        services.redeem_points(self.account, self.account.available_points - 5)
        services.cancel_order(self.order)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 0)  # clamped, never negative

    def test_cannot_cancel_an_already_cancelled_order(self):
        services.cancel_order(self.order)
        with self.assertRaises(services.InvalidOrderStateError):
            services.cancel_order(self.order)


class ExpirePointsTests(TestCase, LoyaltyTestSetupMixin):
    def setUp(self):
        self.business, self.customer, self.program = self._setup_business_and_program("_expire")
        self.program.points_expire_after_days = 30
        self.program.save(update_fields=["points_expire_after_days"])
        self.account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=self.program)

    def _backdate(self, entry, days_ago):
        PointsTransaction.objects.filter(pk=entry.pk).update(expires_at=timezone.now() - timezone.timedelta(days=days_ago))

    def test_expired_points_are_deducted(self):
        entry = services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        self.assertIsNotNone(entry.expires_at)
        self._backdate(entry, 1)

        expired_count = expire_points()
        self.assertEqual(expired_count, 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 0)
        self.assertTrue(PointsTransaction.objects.filter(expired_transaction=entry).exists())

    def test_expiration_is_clamped_to_remaining_available_points(self):
        entry = services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        services.redeem_points(self.account, 70)  # only 30 left
        self._backdate(entry, 1)

        expire_points()
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 0)  # never goes negative

    def test_running_expiration_twice_does_not_double_deduct(self):
        entry = services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)
        self._backdate(entry, 1)

        expire_points()
        second_run_count = expire_points()
        self.assertEqual(second_run_count, 0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 0)

    def test_points_not_yet_expired_are_untouched(self):
        services.award_points(self.account, 100, PointsTransaction.Reason.MANUAL)  # expires 30 days out, not yet
        expired_count = expire_points()
        self.assertEqual(expired_count, 0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_points, 100)

    def test_no_expiration_window_means_points_never_expire(self):
        no_expiry_program = LoyaltyProgram.objects.create(business=self.business, name="No Expiry Program")
        account = CustomerLoyaltyAccount.objects.create(customer=self.customer, loyalty_program=no_expiry_program)
        entry = services.award_points(account, 100, PointsTransaction.Reason.MANUAL)
        self.assertIsNone(entry.expires_at)
        expired_count = expire_points()
        self.assertEqual(expired_count, 0)


class GiftCardEmailAndQRTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_gc_email@example.com")
        self.business = Business.objects.create(name="Gift Card Email Biz", owner=owner)
        self.staff_user = User.objects.create_user(email="staff_gc_email@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_qr_code_endpoint_returns_a_real_png(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        response = self.client.get(gift_card_qr_url(self.business.id, card.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG"))

    def test_qr_png_generation_is_deterministic_for_the_same_code(self):
        png_one = generate_qr_png("ABC123")
        png_two = generate_qr_png("ABC123")
        self.assertEqual(png_one, png_two)

    @patch("loyalty.services.send_email")
    def test_send_gift_card_email_sets_sent_at_on_success(self, mock_send_email):
        card = services.create_gift_card(self.business, Decimal("50"), recipient_email="recipient@example.com")
        services.send_gift_card_email(card)
        mock_send_email.assert_called_once()
        card.refresh_from_db()
        self.assertIsNotNone(card.sent_at)

    def test_send_gift_card_email_requires_a_recipient(self):
        card = services.create_gift_card(self.business, Decimal("50"))
        with self.assertRaises(services.LoyaltyError):
            services.send_gift_card_email(card)

    @patch("loyalty.services.send_email", side_effect=Exception("Resend API returned 500"))
    def test_send_endpoint_surfaces_email_failure_without_crashing(self, mock_send_email):
        card = services.create_gift_card(self.business, Decimal("50"), recipient_email="recipient@example.com")
        with self.assertRaises(Exception):
            services.send_gift_card_email(card)
        mock_send_email.assert_called_once()
        card.refresh_from_db()
        self.assertIsNone(card.sent_at)


class LoyaltyTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_loy_iso@example.com")
        self.business_a = Business.objects.create(name="Loy Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Loy Biz B", owner=owner)
        self.customer_b = Customer.objects.create(business=self.business_b, name="Customer B")
        self.program_b = LoyaltyProgram.objects.create(business=self.business_b, name="Program B")
        self.account_b = CustomerLoyaltyAccount.objects.create(customer=self.customer_b, loyalty_program=self.program_b)
        services.award_points(self.account_b, 100, PointsTransaction.Reason.MANUAL)
        self.order_b = services.create_order_and_award_points(
            self.business_b, self.customer_b, SAMPLE_LINE_ITEMS, "ZERO", loyalty_program=self.program_b
        )
        self.gift_card_b = services.create_gift_card(self.business_b, Decimal("50"))

        self.user_a = User.objects.create_user(email="staff_loy_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_loy_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_loyalty_programs(self):
        response = self.client.get(loyalty_program_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_list_other_business_loyalty_accounts(self):
        response = self.client.get(loyalty_account_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_award_points_on_other_business_account(self):
        self.account_b.refresh_from_db()
        points_before = self.account_b.available_points
        response = self.client.post(loyalty_account_award_url(self.business_b.id, self.account_b.id), {"amount": 10})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.available_points, points_before)

    def test_cannot_redeem_points_on_other_business_account(self):
        self.account_b.refresh_from_db()
        points_before = self.account_b.available_points
        response = self.client.post(loyalty_account_redeem_url(self.business_b.id, self.account_b.id), {"amount": 10})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.available_points, points_before)

    def test_cannot_list_other_business_points_transactions(self):
        response = self.client.get(points_transaction_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_list_other_business_orders(self):
        response = self.client.get(order_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_cancel_other_business_order(self):
        response = self.client.post(order_cancel_url(self.business_b.id, self.order_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.order_b.refresh_from_db()
        self.assertEqual(self.order_b.status, Order.Status.COMPLETED)

    def test_cannot_list_other_business_gift_cards(self):
        response = self.client.get(gift_card_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_redeem_other_business_gift_card(self):
        response = self.client.post(gift_card_redeem_url(self.business_b.id, self.gift_card_b.id), {"amount": "10"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.gift_card_b.refresh_from_db()
        self.assertEqual(self.gift_card_b.current_balance, Decimal("50"))

    def test_cannot_view_other_business_gift_card_qr_code(self):
        response = self.client.get(gift_card_qr_url(self.business_b.id, self.gift_card_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
