"""
Cross-domain regression/consistency pass — the interactions *between*
domains, which each domain's own test suite (built and verified one at a
time) never exercised. Organized into the 6 areas audited together with
the user; each test proves *actual current behavior*, not inferred
behavior. See the session's chat for the full write-up of which findings
are flagged as product decisions (not touched here) vs. fixed directly
(loyalty's CASCADE->PROTECT changes + the new global ProtectedError
handler, both exercised below).
"""

from decimal import Decimal

from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from customers.models import Customer
from documents.models import Document
from employees.models import EmployeeShift, Position, ShiftTemplate
from finance import recurring as finance_recurring
from finance import services as finance_services
from finance.models import Invoice, RecurringTransaction
from inventory.models import Vendor
from loyalty import services as loyalty_services
from loyalty.models import CustomerLoyaltyAccount, GiftCardTransaction, LoyaltyProgram, Order, PointsTransaction
from reservations.models import Reservation

from .models import Business, BusinessMembership

SAMPLE_LINE_ITEMS = [{"description": "Widget", "quantity": Decimal("1"), "unit_price": Decimal("100")}]


# --- Area 1: Order cancellation cross-domain effects -------------------------------


class OrderCancellationCrossDomainTests(TestCase):
    """
    Decision (resolved after the cross-domain audit flagged it as
    undecided): Order and Invoice are now linkable via
    services.convert_order_to_invoice (see loyalty/test_order_invoice_conversion.py
    for the conversion path itself, tested end-to-end there). This file
    keeps the narrower regression: even for a *converted* Order, cancelling
    it never touches the linked Invoice — they're independent once linked
    (see convert_order_to_invoice's docstring for why).
    """

    def setUp(self):
        owner = User.objects.create_user(email="order-invoice@example.com")
        self.business = Business.objects.create(name="Order/Invoice Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.program = LoyaltyProgram.objects.create(business=self.business, name="Program")

    def test_order_can_now_be_linked_to_an_invoice(self):
        field_names = {f.name for f in Order._meta.get_fields()}
        self.assertIn("invoice", field_names)

    def test_cancelling_an_unconverted_order_does_not_touch_an_unrelated_invoice(self):
        invoice = finance_services.create_invoice(
            self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO"
        )
        order = loyalty_services.create_order_and_award_points(
            self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO", loyalty_program=self.program
        )
        original_status = invoice.status

        loyalty_services.cancel_order(order)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, original_status)

    def test_cancelling_a_converted_order_does_not_touch_its_own_linked_invoice(self):
        order = loyalty_services.create_order_and_award_points(
            self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO", loyalty_program=self.program
        )
        invoice = loyalty_services.convert_order_to_invoice(order)
        finance_services.send_invoice(invoice)
        finance_services.record_payment(invoice, invoice.total, "cash")

        loyalty_services.cancel_order(order)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    def test_cancelling_an_order_reverses_its_own_points_but_nothing_else(self):
        """The one real cross-domain effect order cancellation DOES have — confirmed end-to-end, not just at the unit level."""
        order = loyalty_services.create_order_and_award_points(
            self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO", loyalty_program=self.program
        )
        account = CustomerLoyaltyAccount.objects.get(customer=self.customer, loyalty_program=self.program)
        self.assertGreater(account.available_points, 0)

        loyalty_services.cancel_order(order)

        account.refresh_from_db()
        self.assertEqual(account.available_points, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)


# --- Area 2: Customer deletion / deactivation cascades -----------------------------


class CustomerDeletionCascadeTests(TestCase):
    """
    Traces what actually happens — not Django's documented default, the
    actual behavior in this schema — to each of Reservation,
    CustomerLoyaltyAccount, Order, and Document when a Customer is
    deleted.
    """

    def setUp(self):
        owner = User.objects.create_user(email="customer-delete@example.com")
        self.business = Business.objects.create(name="Customer Delete Biz", owner=owner)
        self.program = LoyaltyProgram.objects.create(business=self.business, name="Program")

    def test_reservation_has_no_relationship_to_customer_at_all(self):
        """Reservations are guest_name/guest_email/guest_phone free text — never linked to a Customer row."""
        field_names = {f.name for f in Reservation._meta.get_fields()}
        self.assertNotIn("customer", field_names)

    def test_document_has_no_relationship_to_customer_at_all(self):
        field_names = {f.name for f in Document._meta.get_fields()}
        self.assertNotIn("customer", field_names)

    def test_deleting_a_customer_with_no_history_succeeds(self):
        customer = Customer.objects.create(business=self.business, name="Plain Customer")
        customer.delete()
        self.assertFalse(Customer.objects.filter(name="Plain Customer").exists())

    def test_deleting_a_customer_with_an_order_is_blocked(self):
        """Order.customer is PROTECT — already correctly chosen before this audit."""
        customer = Customer.objects.create(business=self.business, name="Ordering Customer")
        loyalty_services.create_order_and_award_points(self.business, customer, SAMPLE_LINE_ITEMS, "ZERO")
        with self.assertRaises(ProtectedError):
            customer.delete()
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_deleting_a_customer_with_loyalty_history_is_now_blocked(self):
        """
        Before this audit's fix, CustomerLoyaltyAccount.customer was
        CASCADE: deleting the Customer would silently wipe the account
        AND every PointsTransaction in it via raw SQL, completely
        bypassing PointsTransaction.delete()'s own append-only block
        (cascades never call instance .delete()). Now PROTECT, matching
        every other Customer-FK'd financial/ledger model in this codebase
        (Order, Invoice, Estimate).
        """
        customer = Customer.objects.create(business=self.business, name="Loyal Customer")
        account = CustomerLoyaltyAccount.objects.create(customer=customer, loyalty_program=self.program)
        loyalty_services.award_points(account, 100, PointsTransaction.Reason.MANUAL)

        with self.assertRaises(ProtectedError):
            customer.delete()

        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())
        self.assertTrue(CustomerLoyaltyAccount.objects.filter(pk=account.pk).exists())
        self.assertEqual(PointsTransaction.objects.filter(account=account).count(), 1)

    def test_deactivating_a_customer_is_the_supported_alternative_to_deleting(self):
        """is_active=False is always safe regardless of history — the documented way to retire a Customer."""
        customer = Customer.objects.create(business=self.business, name="To Retire")
        account = CustomerLoyaltyAccount.objects.create(customer=customer, loyalty_program=self.program)
        loyalty_services.award_points(account, 50, PointsTransaction.Reason.MANUAL)

        customer.is_active = False
        customer.save(update_fields=["is_active"])

        customer.refresh_from_db()
        self.assertFalse(customer.is_active)
        self.assertTrue(CustomerLoyaltyAccount.objects.filter(pk=account.pk).exists())


# --- Area 3: BusinessMembership deactivation (employee leaving) --------------------


class MembershipDeactivationTests(TestCase):
    """
    Decision (resolved after the cross-domain audit flagged the
    open-TimeEntry/pending-request survival as undecided): deactivation
    now auto-resolves all of it — see employees/test_deactivation.py for
    the full cascade test suite (open TimeEntry/TimeEntryBreak force-closed,
    pending ShiftSwapRequest/TimeOffRequest cancelled, including the
    swap-as-target case, idempotency, and the signal wiring itself). This
    file keeps only the access-denial regression, since it's a
    core.permissions concern, not an employees one.
    """

    def setUp(self):
        self.owner = User.objects.create_user(email="membership-deactivation-owner@example.com")
        self.business = Business.objects.create(name="Deactivation Biz", owner=self.owner)
        self.employee_user = User.objects.create_user(email="leaving-employee@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=self.employee_user, role=BusinessMembership.Role.STAFF
        )

    def test_deactivation_denies_access_immediately_even_mid_session(self):
        """
        Simulates the exact scenario asked about: a user with a still-valid
        Supabase JWT (force_authenticate never expires, modeling that) gets
        deactivated by an admin mid-session. HasBusinessRole re-queries
        BusinessMembership fresh on every single request (core/permissions.py),
        so there is nothing cached anywhere to invalidate — the very next
        request after deactivation is denied, regardless of token validity.
        """
        client = APIClient()
        client.force_authenticate(user=self.employee_user)

        response = client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 200)

        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])

        # Same client, same "session" (no re-authentication, same simulated JWT).
        response = client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 403)


# --- Area 4: Payment/refund reversal ------------------------------------------------


class PaymentRefundReversalTests(TestCase):
    """
    Decision (resolved after the cross-domain audit flagged the
    "refunds deferred to part 2, never built" gap): a real refund
    mechanism now exists — see finance/test_refunds.py for the full
    suite (partial/full refund, over-refund rejection, status transitions,
    concurrency, tenant isolation). This file keeps the narrower
    regressions: Payment itself still can't be edited/deleted (a Refund
    is a separate row, never a mutation of the original), and finance
    still has no *structural* (import/call) coupling to loyalty — only
    its own prose now mentions the word, explaining there isn't one.
    """

    def setUp(self):
        owner = User.objects.create_user(email="refund-test@example.com")
        self.business = Business.objects.create(name="Refund Test Biz", owner=owner)
        self.customer = Customer.objects.create(business=self.business, name="Customer")
        self.staff_user = User.objects.create_user(email="refund-staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_payment_cannot_be_edited_or_deleted_via_the_model_layer(self):
        invoice = finance_services.create_invoice(self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO")
        finance_services.send_invoice(invoice)
        payment = finance_services.record_payment(invoice, Decimal("100.00"), "cash")
        with self.assertRaises(TypeError):
            payment.delete()
        with self.assertRaises(TypeError):
            payment.notes = "tampered"
            payment.save()

    def test_payment_delete_route_is_still_not_exposed_via_the_api(self):
        invoice = finance_services.create_invoice(self.business, self.customer, SAMPLE_LINE_ITEMS, "ZERO")
        finance_services.send_invoice(invoice)
        payment = finance_services.record_payment(invoice, Decimal("100.00"), "cash")
        response = self.client.delete(f"/api/businesses/{self.business.id}/payments/{payment.id}/")
        self.assertEqual(response.status_code, 405)

    def test_no_structural_finance_to_loyalty_coupling_exists(self):
        """
        No name in this module is actually defined in (imported from)
        loyalty — a plain text search over source would false-positive on
        this module's own explanatory docstrings/comments (which now
        mention "loyalty" precisely to document that no coupling exists),
        so this checks __module__ on every public attribute instead.
        """
        for name in dir(finance_services):
            if name.startswith("_"):
                continue
            module = getattr(getattr(finance_services, name), "__module__", "") or ""
            self.assertFalse(module.startswith("loyalty"), f"{name} unexpectedly comes from loyalty ({module})")


# --- Area 5: Recurring generation against a lapsed subscription --------------------


class RecurringGenerationVsLapsedSubscriptionTests(TestCase):
    """
    Fixed (unambiguous, not a judgment call — the cross-domain audit
    flagged this as a decision point, and the decision was "this should
    not happen"): expand_active_recurring_transactions (finance) and
    expand_active_recurring_schedules (employees) now both exclude
    deactivated/lapsed businesses, and HasBusinessRole now denies all
    business-scoped access once Business.is_active is False. See
    core/test_business_activation.py for the full suite (including the
    mid-session-denial proof, mirroring the membership-deactivation test).
    This file keeps the narrower regression: a lapsed business's
    recurring schedules/transactions stay inert.
    """

    def setUp(self):
        owner = User.objects.create_user(email="lapsed-sub@example.com")
        self.business = Business.objects.create(
            name="Lapsed Subscription Biz", owner=owner, is_active=False, subscription_status="canceled"
        )
        self.customer = Customer.objects.create(business=self.business, name="Customer")

    def test_recurring_transaction_expansion_skips_a_deactivated_business(self):
        RecurringTransaction.objects.create(
            business=self.business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=self.customer,
            line_item_presets=[{"description": "Subscription", "quantity": "1", "unit_price": "50.00"}],
            tax_type="ZERO",
            recurrence_rule=RecurringTransaction.Recurrence.MONTHLY,
            start_date=timezone.now().date(),
        )
        created_count = finance_recurring.expand_active_recurring_transactions()
        self.assertEqual(created_count, 0)
        self.assertFalse(Invoice.objects.filter(business=self.business).exists())

    def test_recurring_schedule_expansion_skips_a_deactivated_business(self):
        from employees import scheduling as employees_scheduling
        from employees.models import RecurringSchedule

        membership = BusinessMembership.objects.create(
            business=self.business,
            user=User.objects.create_user(email="lapsed-sub-employee@example.com"),
            role=BusinessMembership.Role.STAFF,
        )
        position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))
        template = ShiftTemplate.objects.create(
            business=self.business, position=position, day_of_week=timezone.now().date().weekday(),
            start_time="09:00", end_time="17:00",
        )
        RecurringSchedule.objects.create(
            membership=membership,
            shift_template=template,
            recurrence_rule=RecurringSchedule.Recurrence.WEEKLY,
            start_date=timezone.now().date(),
        )
        created_count = employees_scheduling.expand_active_recurring_schedules()
        self.assertEqual(created_count, 0)
        self.assertFalse(EmployeeShift.objects.filter(membership=membership).exists())

    def test_business_is_active_false_now_blocks_normal_api_access(self):
        staff_user = User.objects.create_user(email="lapsed-sub-staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=staff_user, role=BusinessMembership.Role.STAFF
        )
        client = APIClient()
        client.force_authenticate(user=staff_user)
        response = client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 403)


# --- Area 6: on_delete sweep — verifying the fixes made during this audit ----------


class ProtectedDeleteCleanErrorTests(TestCase):
    """
    Separately from the CASCADE->PROTECT model fixes themselves
    (exercised in CustomerDeletionCascadeTests above), this confirms the
    other half of that fix: ProtectedError previously had no handling
    anywhere in this codebase (grepped — zero hits), so even the
    *already-correct* PROTECT relations (Order.customer, Invoice.customer,
    Bill.vendor, ...) surfaced as a raw 500 instead of a clean 400. The
    new core.exceptions.exception_handler (wired in via
    REST_FRAMEWORK.EXCEPTION_HANDLER) fixes that for every PROTECT
    relation in the system at once, not just loyalty's.
    """

    def setUp(self):
        owner = User.objects.create_user(email="protected-delete@example.com")
        self.business = Business.objects.create(name="Protected Delete Biz", owner=owner)
        self.staff_user = User.objects.create_user(email="protected-delete-staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_deleting_a_customer_with_an_order_returns_400_not_500(self):
        customer = Customer.objects.create(business=self.business, name="Has An Order")
        loyalty_services.create_order_and_award_points(self.business, customer, SAMPLE_LINE_ITEMS, "ZERO")

        response = self.client.delete(f"/api/businesses/{self.business.id}/customers/{customer.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertTrue(Customer.objects.filter(pk=customer.id).exists())

    def test_deleting_a_vendor_with_a_bill_returns_400_not_500(self):
        vendor = Vendor.objects.create(business=self.business, name="Has A Bill")
        finance_services.create_bill(
            self.business, vendor, SAMPLE_LINE_ITEMS, "ZERO"
        )
        response = self.client.delete(f"/api/businesses/{self.business.id}/vendors/{vendor.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Vendor.objects.filter(pk=vendor.id).exists())

    def test_deleting_a_loyalty_program_with_an_enrolled_account_returns_400_not_500(self):
        program = LoyaltyProgram.objects.create(business=self.business, name="Program")
        customer = Customer.objects.create(business=self.business, name="Enrolled Customer")
        CustomerLoyaltyAccount.objects.create(customer=customer, loyalty_program=program)

        response = self.client.delete(f"/api/businesses/{self.business.id}/loyalty-programs/{program.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(LoyaltyProgram.objects.filter(pk=program.id).exists())

    def test_deleting_a_loyalty_program_with_no_accounts_succeeds(self):
        """PROTECT is conditional — a genuinely unused program is still freely deletable."""
        program = LoyaltyProgram.objects.create(business=self.business, name="Unused Program")
        response = self.client.delete(f"/api/businesses/{self.business.id}/loyalty-programs/{program.id}/")
        self.assertEqual(response.status_code, 204)

    def test_deleting_a_gift_card_is_always_blocked_since_it_always_has_a_transaction(self):
        """create_gift_card always creates an INITIAL transaction — every gift card is permanent from creation."""
        card = loyalty_services.create_gift_card(self.business, Decimal("50"))
        response = self.client.delete(f"/api/businesses/{self.business.id}/gift-cards/{card.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(GiftCardTransaction.objects.filter(gift_card=card).exists())

    def test_deleting_an_unused_customer_loyalty_account_succeeds(self):
        """Zero transactions (never awarded anything) — still freely deletable, same conditional PROTECT."""
        program = LoyaltyProgram.objects.create(business=self.business, name="Program")
        customer = Customer.objects.create(business=self.business, name="Customer")
        account = CustomerLoyaltyAccount.objects.create(customer=customer, loyalty_program=program)

        response = self.client.delete(f"/api/businesses/{self.business.id}/loyalty-accounts/{account.id}/")
        self.assertEqual(response.status_code, 204)
