"""
Fix 1 (from the cross-domain audit, unambiguous — not a judgment call):
Business.is_active was written correctly (core.tasks.check_expired_trials,
finance.webhooks' Stripe handler) but read by nothing. A deactivated/lapsed
business's staff retained full API access, and the recurring expansion
tasks kept generating new Invoices/Bills/Shifts for it forever. Fixed in
core/permissions.py (HasBusinessRole) and finance/recurring.py +
employees/scheduling.py.

Fix 2: Business has no supported delete path anywhere — Django admin's
BusinessAdmin.has_delete_permission now always returns False, and there
is (and was) no API endpoint for Business at all.
"""

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from core.admin import BusinessAdmin
from core.permissions import HasBusinessRole
from customers.models import Customer
from finance import recurring as finance_recurring
from finance.models import Invoice, RecurringTransaction

from .models import Business, BusinessMembership


class FakeRequest:
    """has_object_permission only reads request.user — see core/test_permissions.py."""

    def __init__(self, user):
        self.user = user


class BusinessIsActiveDeniesAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="biz-active-owner@example.com")
        self.business = Business.objects.create(name="Active Gate Biz", owner=self.owner)
        self.staff_user = User.objects.create_user(email="biz-active-staff@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_active_business_allows_access(self):
        response = self.client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 200)

    def test_deactivating_the_business_denies_access_immediately_even_mid_session(self):
        """Mirrors the membership-deactivation mid-session test exactly — same mechanism, the Business row instead of the membership row."""
        response = self.client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 200)

        self.business.is_active = False
        self.business.save(update_fields=["is_active"])

        # Same client, same simulated still-valid session — no re-authentication.
        response = self.client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 403)

    def test_reactivating_the_business_restores_access(self):
        self.business.is_active = False
        self.business.save(update_fields=["is_active"])
        response = self.client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 403)

        self.business.is_active = True
        self.business.save(update_fields=["is_active"])
        response = self.client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 200)

    def test_inactive_business_blocks_object_level_access_too(self):
        customer = Customer.objects.create(business=self.business, name="Someone")
        self.business.is_active = False
        self.business.save(update_fields=["is_active"])

        response = self.client.get(f"/api/businesses/{self.business.id}/customers/{customer.id}/")
        self.assertEqual(response.status_code, 403)

    def test_has_object_permission_directly_denies_for_inactive_business(self):
        self.business.is_active = False
        self.business.save(update_fields=["is_active"])
        customer = Customer.objects.create(business=self.business, name="Someone")

        permission = HasBusinessRole()
        self.assertFalse(permission.has_object_permission(FakeRequest(self.staff_user), None, customer))

    def test_superadmin_bypasses_the_is_active_check(self):
        self.business.is_active = False
        self.business.save(update_fields=["is_active"])
        superadmin = User.objects.create_user(email="biz-active-superadmin@example.com", is_superadmin=True)
        client = APIClient()
        client.force_authenticate(user=superadmin)

        response = client.get(f"/api/businesses/{self.business.id}/customers/")
        self.assertEqual(response.status_code, 200)


class BusinessNonDeletableTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="biz-nondelete-owner@example.com")
        self.business = Business.objects.create(name="Non-Deletable Biz", owner=owner)

    def test_business_admin_has_delete_permission_is_always_false(self):
        admin_instance = BusinessAdmin(Business, AdminSite())
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None, obj=self.business))

    def test_there_is_no_api_endpoint_for_business_at_all(self):
        """No core/urls.py or core/views.py exists — Business is only ever reached indirectly via business_id in other apps' URLs."""
        import importlib

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("core.urls")


class RecurringExpansionRespectsBusinessActiveTests(TestCase):
    """Positive control alongside core/test_cross_domain_consistency.py's negative case — confirms the new filter doesn't accidentally exclude active businesses too."""

    def test_recurring_transaction_expansion_still_works_for_an_active_business(self):
        owner = User.objects.create_user(email="active-recurring-owner@example.com")
        business = Business.objects.create(name="Active Recurring Biz", owner=owner, is_active=True)
        customer = Customer.objects.create(business=business, name="Customer")
        RecurringTransaction.objects.create(
            business=business,
            kind=RecurringTransaction.Kind.INVOICE,
            customer=customer,
            line_item_presets=[{"description": "Subscription", "quantity": "1", "unit_price": "50.00"}],
            tax_type="ZERO",
            recurrence_rule=RecurringTransaction.Recurrence.MONTHLY,
            start_date=timezone.now().date(),
        )
        created_count = finance_recurring.expand_active_recurring_transactions()
        self.assertGreater(created_count, 0)
        self.assertTrue(Invoice.objects.filter(business=business).exists())
