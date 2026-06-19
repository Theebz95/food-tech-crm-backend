"""
Tests for the Inventory domain: CRUD, the adjust_stock concurrency fix,
negative-stock rejection, ledger immutability, and tenant isolation
(same rigor as prior domains).
"""

import threading
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessLocation, BusinessMembership

from . import services
from .models import InventoryItem, InventoryTransaction, Vendor


def vendor_list_url(business_id):
    return f"/api/businesses/{business_id}/vendors/"


def item_list_url(business_id):
    return f"/api/businesses/{business_id}/inventory-items/"


def item_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/inventory-items/{pk}/"


def item_low_stock_url(business_id):
    return f"/api/businesses/{business_id}/inventory-items/low-stock/"


def item_adjust_stock_url(business_id, pk):
    return f"/api/businesses/{business_id}/inventory-items/{pk}/adjust-stock/"


def transaction_list_url(business_id):
    return f"/api/businesses/{business_id}/inventory-transactions/"


class InventoryItemCRUDTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_inv@example.com")
        self.business = Business.objects.create(name="Inventory Biz", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff_inv@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_item_with_starting_quantity(self):
        response = self.client.post(
            item_list_url(self.business.id),
            {"name": "Flour", "unit": "kg", "current_quantity": "10.000", "low_stock_threshold": "2.000"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = InventoryItem.objects.get(name="Flour")
        self.assertEqual(item.current_quantity, Decimal("10.000"))

    def test_list_and_retrieve(self):
        item = InventoryItem.objects.create(business=self.business, name="Sugar", unit="kg", current_quantity=5)
        list_response = self.client.get(item_list_url(self.business.id))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)

        detail_response = self.client.get(item_detail_url(self.business.id, item.id))
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["name"], "Sugar")

    def test_update_non_quantity_fields_succeeds(self):
        item = InventoryItem.objects.create(business=self.business, name="Salt", unit="kg", current_quantity=5)
        response = self.client.patch(item_detail_url(self.business.id, item.id), {"low_stock_threshold": "1.5"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item.refresh_from_db()
        self.assertEqual(item.low_stock_threshold, Decimal("1.5"))

    def test_update_cannot_change_quantity_directly(self):
        item = InventoryItem.objects.create(business=self.business, name="Pepper", unit="kg", current_quantity=5)
        response = self.client.patch(item_detail_url(self.business.id, item.id), {"current_quantity": "99.000"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.current_quantity, Decimal("5"))

    def test_delete_item(self):
        item = InventoryItem.objects.create(business=self.business, name="Butter", unit="kg", current_quantity=5)
        response = self.client.delete(item_detail_url(self.business.id, item.id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(InventoryItem.objects.filter(id=item.id).exists())

    def test_create_vendor_and_assign_to_item(self):
        vendor_response = self.client.post(vendor_list_url(self.business.id), {"name": "Acme Supply"})
        self.assertEqual(vendor_response.status_code, status.HTTP_201_CREATED, vendor_response.data)
        vendor_id = vendor_response.data["id"]

        item_response = self.client.post(
            item_list_url(self.business.id), {"name": "Oil", "unit": "liter", "vendor": vendor_id}
        )
        self.assertEqual(item_response.status_code, status.HTTP_201_CREATED, item_response.data)
        self.assertEqual(str(item_response.data["vendor"]), vendor_id)


class AdjustStockTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_adjust@example.com")
        self.business = Business.objects.create(name="Adjust Biz", owner=self.owner)
        self.location = BusinessLocation.objects.create(business=self.business, name="Main")
        self.staff_user = User.objects.create_user(email="staff_adjust@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.item = InventoryItem.objects.create(
            business=self.business, name="Rice", unit="kg", current_quantity=Decimal("10")
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_restock_increases_quantity_and_creates_ledger_entry(self):
        entry = services.adjust_stock(
            self.item, Decimal("5"), InventoryTransaction.TransactionType.RESTOCK, self.membership, reason="delivery"
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_quantity, Decimal("15"))
        self.assertEqual(entry.quantity_change, Decimal("5"))
        self.assertEqual(entry.transaction_type, InventoryTransaction.TransactionType.RESTOCK)
        self.assertEqual(entry.created_by, self.membership)

    def test_usage_decreases_quantity(self):
        services.adjust_stock(self.item, Decimal("-4"), InventoryTransaction.TransactionType.USAGE, self.membership)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_quantity, Decimal("6"))

    def test_adjustment_that_would_go_negative_is_rejected(self):
        with self.assertRaises(services.InsufficientStockError):
            services.adjust_stock(
                self.item, Decimal("-20"), InventoryTransaction.TransactionType.USAGE, self.membership
            )
        self.item.refresh_from_db()
        # Rejected outright — quantity unchanged, no ledger row created.
        self.assertEqual(self.item.current_quantity, Decimal("10"))
        self.assertEqual(InventoryTransaction.objects.filter(item=self.item).count(), 0)

    def test_adjust_stock_action_via_api(self):
        response = self.client.post(
            item_adjust_stock_url(self.business.id, self.item.id),
            {"delta": "3", "transaction_type": "restock", "reason": "extra delivery"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["item"]["current_quantity"], "13.000")
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_quantity, Decimal("13"))

    def test_adjust_stock_action_rejects_negative_result(self):
        response = self.client.post(
            item_adjust_stock_url(self.business.id, self.item.id),
            {"delta": "-50", "transaction_type": "usage"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_quantity, Decimal("10"))

    def test_ledger_visible_via_read_only_endpoint(self):
        services.adjust_stock(self.item, Decimal("2"), InventoryTransaction.TransactionType.RESTOCK, self.membership)
        response = self.client.get(transaction_list_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_ledger_has_no_write_endpoints(self):
        # No create — POSTing directly to the ledger list isn't a route at all.
        response = self.client.post(transaction_list_url(self.business.id), {"item": str(self.item.id)})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class LedgerImmutabilityTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_ledger@example.com")
        self.business = Business.objects.create(name="Ledger Biz", owner=owner)
        user = User.objects.create_user(email="staff_ledger@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.item = InventoryItem.objects.create(
            business=self.business, name="Yeast", unit="kg", current_quantity=Decimal("5")
        )
        self.entry = services.adjust_stock(
            self.item, Decimal("2"), InventoryTransaction.TransactionType.RESTOCK, self.membership
        )

    def test_cannot_edit_existing_transaction(self):
        self.entry.reason = "tampered"
        with self.assertRaises(TypeError):
            self.entry.save()

    def test_cannot_delete_existing_transaction(self):
        with self.assertRaises(TypeError):
            self.entry.delete()
        self.assertTrue(InventoryTransaction.objects.filter(pk=self.entry.pk).exists())


class StockAdjustmentConcurrencyTests(TransactionTestCase):
    """
    Proves select_for_update() on InventoryItem actually serializes
    adjustments: with quantity=1 and two concurrent -1 deductions, exactly
    one must succeed (quantity -> 0) and the other must be rejected for
    going negative — not both succeeding (lost update) and not both being
    rejected.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_concurrency_inv@example.com")
        self.business = Business.objects.create(name="Concurrency Pantry", owner=owner)
        user = User.objects.create_user(email="staff_concurrency_inv@example.com")
        self.membership = BusinessMembership.objects.create(
            business=self.business, user=user, role=BusinessMembership.Role.STAFF
        )
        self.item = InventoryItem.objects.create(
            business=self.business, name="Saffron", unit="g", current_quantity=Decimal("1")
        )

    def test_only_one_concurrent_deduction_succeeds(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt_deduction():
            barrier.wait()
            try:
                services.adjust_stock(
                    self.item, Decimal("-1"), InventoryTransaction.TransactionType.USAGE, self.membership
                )
                outcome = "success"
            except services.InsufficientStockError:
                outcome = "rejected"
            finally:
                connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt_deduction) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["rejected", "success"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_quantity, Decimal("0"))
        self.assertEqual(InventoryTransaction.objects.filter(item=self.item).count(), 1)


class LowStockTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_lowstock@example.com")
        self.business = Business.objects.create(name="Low Stock Biz", owner=owner)
        self.staff_user = User.objects.create_user(email="staff_lowstock@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_low_stock_lists_only_items_at_or_below_threshold(self):
        InventoryItem.objects.create(
            business=self.business, name="Low Item", unit="kg", current_quantity=2, low_stock_threshold=5
        )
        InventoryItem.objects.create(
            business=self.business, name="Exact Item", unit="kg", current_quantity=5, low_stock_threshold=5
        )
        InventoryItem.objects.create(
            business=self.business, name="Plenty Item", unit="kg", current_quantity=50, low_stock_threshold=5
        )

        response = self.client.get(item_low_stock_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row["name"] for row in response.data}
        self.assertEqual(names, {"Low Item", "Exact Item"})


class InventoryTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_inv_iso@example.com")
        self.business_a = Business.objects.create(name="Inv Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Inv Biz B", owner=owner)

        self.user_a = User.objects.create_user(email="staff_inv_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_inv_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        self.vendor_b = Vendor.objects.create(business=self.business_b, name="Vendor B")
        self.item_b = InventoryItem.objects.create(
            business=self.business_b, name="Item B", unit="kg", current_quantity=5
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_items(self):
        response = self.client.get(item_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_create_item_for_other_business(self):
        response = self.client.post(item_list_url(self.business_b.id), {"name": "Sneaky", "unit": "kg"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(InventoryItem.objects.filter(name="Sneaky").exists())

    def test_cannot_retrieve_other_business_item(self):
        response = self.client.get(item_detail_url(self.business_b.id, self.item_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_other_business_item(self):
        response = self.client.delete(item_detail_url(self.business_b.id, self.item_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(InventoryItem.objects.filter(id=self.item_b.id).exists())

    def test_cannot_assign_a_foreign_vendor_even_through_own_business_url(self):
        response = self.client.post(
            item_list_url(self.business_a.id), {"name": "Sneaky Item", "unit": "kg", "vendor": str(self.vendor_b.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(InventoryItem.objects.filter(name="Sneaky Item").exists())

    def test_cannot_adjust_stock_on_other_business_item(self):
        response = self.client.post(
            item_adjust_stock_url(self.business_b.id, self.item_b.id),
            {"delta": "1", "transaction_type": "restock"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.current_quantity, Decimal("5"))
