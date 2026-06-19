"""
Tests for the Customer CRM domain: CRUD, tenant isolation, and the
server-side email/phone validation called out in serializers.py.
"""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership

from .models import Customer


def customer_list_url(business_id):
    return f"/api/businesses/{business_id}/customers/"


def customer_detail_url(business_id, customer_id):
    return f"/api/businesses/{business_id}/customers/{customer_id}/"


class CustomerCRUDTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com")
        self.business = Business.objects.create(name="Test Cafe", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_customer(self):
        response = self.client.post(
            customer_list_url(self.business.id),
            {"name": "Alice", "email": "alice@example.com", "phone": "+15551234567"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        customer = Customer.objects.get(name="Alice")
        self.assertEqual(customer.business_id, self.business.id)
        self.assertEqual(customer.email, "alice@example.com")

    def test_list_customers(self):
        Customer.objects.create(business=self.business, name="Alice")
        Customer.objects.create(business=self.business, name="Bob")
        response = self.client.get(customer_list_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_retrieve_customer(self):
        customer = Customer.objects.create(business=self.business, name="Alice")
        response = self.client.get(customer_detail_url(self.business.id, customer.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Alice")

    def test_update_customer(self):
        customer = Customer.objects.create(business=self.business, name="Alice")
        response = self.client.patch(
            customer_detail_url(self.business.id, customer.id), {"notes": "Prefers window seating"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        customer.refresh_from_db()
        self.assertEqual(customer.notes, "Prefers window seating")

    def test_delete_customer(self):
        customer = Customer.objects.create(business=self.business, name="Alice")
        response = self.client.delete(customer_detail_url(self.business.id, customer.id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Customer.objects.filter(id=customer.id).exists())


class CustomerValidationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner2@example.com")
        self.business = Business.objects.create(name="Test Cafe 2", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff2@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_rejects_invalid_phone(self):
        response = self.client.post(
            customer_list_url(self.business.id), {"name": "Alice", "phone": "not-a-phone"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("phone", response.data)
        self.assertFalse(Customer.objects.filter(name="Alice").exists())

    def test_create_rejects_invalid_email(self):
        response = self.client.post(
            customer_list_url(self.business.id), {"name": "Alice", "email": "not-an-email"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertFalse(Customer.objects.filter(name="Alice").exists())

    def test_create_rejects_duplicate_email_within_business(self):
        Customer.objects.create(business=self.business, name="Alice", email="dupe@example.com")
        response = self.client.post(
            customer_list_url(self.business.id), {"name": "Alice 2", "email": "dupe@example.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(Customer.objects.filter(email="dupe@example.com").count(), 1)

    def test_create_allows_multiple_customers_with_blank_email(self):
        Customer.objects.create(business=self.business, name="Alice")
        response = self.client.post(customer_list_url(self.business.id), {"name": "Bob"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_update_rejects_duplicate_email_with_another_customer(self):
        Customer.objects.create(business=self.business, name="Alice", email="dupe@example.com")
        bob = Customer.objects.create(business=self.business, name="Bob", email="bob@example.com")
        response = self.client.patch(
            customer_detail_url(self.business.id, bob.id), {"email": "dupe@example.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        bob.refresh_from_db()
        self.assertEqual(bob.email, "bob@example.com")


class CustomerBusinessSmugglingTests(TestCase):
    """
    `business` is read_only on CustomerSerializer specifically so a client
    can't write into a tenant it has no membership for by passing a
    different business id in the request payload (see serializers.py). These
    tests exercise that directly rather than relying on it as a side effect
    of the read_only declaration.
    """

    def setUp(self):
        self.owner = User.objects.create_user(email="owner3@example.com")
        self.business_a = Business.objects.create(name="Business A", owner=self.owner)
        self.business_b = Business.objects.create(name="Business B", owner=self.owner)

        self.staff_user = User.objects.create_user(email="staff3@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        # No membership on business_b for this user.

        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_ignores_business_smuggled_in_payload(self):
        response = self.client.post(
            customer_list_url(self.business_a.id),
            {"name": "Alice", "business": str(self.business_b.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        customer = Customer.objects.get(name="Alice")
        # Despite the payload naming business_b, the customer is created
        # under the business from the URL, which the caller actually has a
        # membership for.
        self.assertEqual(customer.business_id, self.business_a.id)
        self.assertNotEqual(customer.business_id, self.business_b.id)

    def test_update_cannot_move_customer_to_another_business_via_payload(self):
        customer = Customer.objects.create(business=self.business_a, name="Alice")
        response = self.client.patch(
            customer_detail_url(self.business_a.id, customer.id),
            {"business": str(self.business_b.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        customer.refresh_from_db()
        self.assertEqual(customer.business_id, self.business_a.id)


class CustomerTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner4@example.com")
        self.business_a = Business.objects.create(name="Business A", owner=owner)
        self.business_b = Business.objects.create(name="Business B", owner=owner)

        self.user_a = User.objects.create_user(email="staffa@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        # user_a has no membership on business_b.

        other_user_b = User.objects.create_user(email="staffb@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )
        self.other_business_customer = Customer.objects.create(business=self.business_b, name="Carol")

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_customers(self):
        response = self.client.get(customer_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_create_customer_for_other_business(self):
        response = self.client.post(customer_list_url(self.business_b.id), {"name": "Mallory"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Customer.objects.filter(name="Mallory").exists())

    def test_cannot_retrieve_other_business_customer_via_other_business_url(self):
        response = self.client.get(
            customer_detail_url(self.business_b.id, self.other_business_customer.id)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_retrieve_other_business_customer_via_own_business_url(self):
        # Even swapping in the customer's real id under the caller's own
        # business URL must not leak it across tenants.
        response = self.client.get(
            customer_detail_url(self.business_a.id, self.other_business_customer.id)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_update_other_business_customer(self):
        response = self.client.patch(
            customer_detail_url(self.business_b.id, self.other_business_customer.id),
            {"name": "Hacked"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.other_business_customer.refresh_from_db()
        self.assertEqual(self.other_business_customer.name, "Carol")

    def test_cannot_delete_other_business_customer(self):
        response = self.client.delete(
            customer_detail_url(self.business_b.id, self.other_business_customer.id)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Customer.objects.filter(id=self.other_business_customer.id).exists())

    def test_own_business_list_only_shows_own_business_customers(self):
        Customer.objects.create(business=self.business_a, name="Alice")
        response = self.client.get(customer_list_url(self.business_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Alice")
