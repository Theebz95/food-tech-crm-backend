"""
Verifies a foundational assumption that's load-bearing for the entire
tenancy model but had never been tested end-to-end across domains: one
User can hold independent BusinessMembership rows — different roles — at
different businesses simultaneously, and every domain scopes access by
"the business named in this request's URL", never by some singular
"the user's business"/"the user's role" concept.

BusinessMembership's unique constraint is (business, user), not (user,)
alone (core/models.py) — structurally this was always possible. What
hadn't been proven is that the *permission and queryset layers* built on
top of it, across every domain, correctly treat each request
independently rather than assuming one role/one business per user.
"""

from authentication.models import User
from customers.models import Customer
from rest_framework.test import APIClient

from django.test import TestCase

from .models import Business, BusinessLocation, BusinessMembership


class MultiBusinessMembershipTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="multi-membership@example.com")
        self.business_a = Business.objects.create(name="Business A (owner)", owner=self.user)
        self.business_b = Business.objects.create(name="Business B (staff)", owner=self.user)

        BusinessMembership.objects.create(
            business=self.business_a, user=self.user, role=BusinessMembership.Role.OWNER
        )
        BusinessMembership.objects.create(
            business=self.business_b, user=self.user, role=BusinessMembership.Role.STAFF
        )
        self.location_b = BusinessLocation.objects.create(business=self.business_b, name="Main")

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_one_user_has_two_independent_memberships_with_different_roles(self):
        memberships = BusinessMembership.objects.filter(user=self.user)
        self.assertEqual(memberships.count(), 2)
        roles_by_business = {m.business_id: m.role for m in memberships}
        self.assertEqual(roles_by_business[self.business_a.id], BusinessMembership.Role.OWNER)
        self.assertEqual(roles_by_business[self.business_b.id], BusinessMembership.Role.STAFF)

    def test_full_crud_at_owner_business(self):
        # Customers: create, retrieve, update, delete.
        response = self.client.post(f"/api/businesses/{self.business_a.id}/customers/", {"name": "Alice"})
        self.assertEqual(response.status_code, 201, response.data)
        customer_id = response.data["id"]

        response = self.client.get(f"/api/businesses/{self.business_a.id}/customers/{customer_id}/")
        self.assertEqual(response.status_code, 200, response.data)

        response = self.client.patch(
            f"/api/businesses/{self.business_a.id}/customers/{customer_id}/", {"name": "Alice Updated"}
        )
        self.assertEqual(response.status_code, 200, response.data)

        response = self.client.delete(f"/api/businesses/{self.business_a.id}/customers/{customer_id}/")
        self.assertEqual(response.status_code, 204, response.data)

        # Reservations: create a table (needs a BusinessLocation at A).
        location_a = BusinessLocation.objects.create(business=self.business_a, name="Main A")
        response = self.client.post(
            f"/api/businesses/{self.business_a.id}/tables/",
            {"location": str(location_a.id), "name": "T1", "capacity": 4},
        )
        self.assertEqual(response.status_code, 201, response.data)

        # Finance: create an invoice (reuses the customer above's business).
        customer = Customer.objects.create(business=self.business_a, name="Bob")
        response = self.client.post(
            f"/api/businesses/{self.business_a.id}/invoices/",
            {
                "customer": str(customer.id),
                "tax_type": "ZERO",
                "line_items": [{"description": "Widget", "quantity": "1", "unit_price": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        # Loyalty: create a program (full CRUD, no role gate beyond staff).
        response = self.client.post(f"/api/businesses/{self.business_a.id}/loyalty-programs/", {"name": "Program A"})
        self.assertEqual(response.status_code, 201, response.data)

        # Owner-level (manager-gated) action: geofence settings.
        response = self.client.post(
            f"/api/businesses/{self.business_a.id}/geofence-settings/",
            {"center_latitude": "1.000000", "center_longitude": "1.000000", "radius_meters": 100},
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_staff_role_at_business_b_allows_staff_actions_but_denies_manager_actions(self):
        # Staff-level CRUD (default HasBusinessRole) is allowed.
        response = self.client.post(f"/api/businesses/{self.business_b.id}/customers/", {"name": "Carol"})
        self.assertEqual(response.status_code, 201, response.data)

        response = self.client.post(
            f"/api/businesses/{self.business_b.id}/tables/",
            {"location": str(self.location_b.id), "name": "T1", "capacity": 4},
        )
        self.assertEqual(response.status_code, 201, response.data)

        # Manager-gated action (IsBusinessManager) is denied — same user,
        # same request session, just a different business in the URL.
        response = self.client.post(
            f"/api/businesses/{self.business_b.id}/geofence-settings/",
            {"center_latitude": "1.000000", "center_longitude": "1.000000", "radius_meters": 100},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_owner_role_at_a_does_not_leak_into_manager_gated_action_at_b(self):
        """The exact same user who is OWNER at A is only STAFF at B — that must not upgrade them at B."""
        response = self.client.post(
            f"/api/businesses/{self.business_b.id}/geofence-settings/",
            {"center_latitude": "1.000000", "center_longitude": "1.000000", "radius_meters": 100},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_customer_lists_do_not_leak_across_businesses_in_either_direction(self):
        Customer.objects.create(business=self.business_a, name="OnlyAtA")
        Customer.objects.create(business=self.business_b, name="OnlyAtB")

        response_a = self.client.get(f"/api/businesses/{self.business_a.id}/customers/")
        names_a = {row["name"] for row in response_a.data}
        self.assertIn("OnlyAtA", names_a)
        self.assertNotIn("OnlyAtB", names_a)

        response_b = self.client.get(f"/api/businesses/{self.business_b.id}/customers/")
        names_b = {row["name"] for row in response_b.data}
        self.assertIn("OnlyAtB", names_b)
        self.assertNotIn("OnlyAtA", names_b)

    def test_object_from_one_business_is_not_reachable_through_the_others_url_prefix(self):
        customer_a = Customer.objects.create(business=self.business_a, name="BelongsToA")
        response = self.client.get(f"/api/businesses/{self.business_b.id}/customers/{customer_a.id}/")
        self.assertEqual(response.status_code, 404, response.data)


class EmployeeMembershipAndCustomerProfileAreIndependentTests(TestCase):
    """
    Confirms there's no incorrect coupling between "is staff somewhere"
    (BusinessMembership) and "is a customer somewhere" (Customer /
    CustomerProfile + CustomerBusinessLink) — including the same person
    being a customer of a business they also work at. See
    customers/models.py: Customer and CustomerProfile are deliberately not
    FK'd to each other or to BusinessMembership.
    """

    def setUp(self):
        self.user = User.objects.create_user(email="dual-role@example.com")
        self.business_a = Business.objects.create(name="Employer Business", owner=self.user)
        self.business_b = Business.objects.create(name="Other Business", owner=self.user)

    def test_staff_member_can_also_have_a_customer_profile_at_a_different_business(self):
        from customers.models import CustomerBusinessLink, CustomerProfile

        BusinessMembership.objects.create(
            business=self.business_a, user=self.user, role=BusinessMembership.Role.STAFF
        )
        profile = CustomerProfile.objects.create(user=self.user, full_name="Dana")
        CustomerBusinessLink.objects.create(customer_profile=profile, business=self.business_b)

        self.assertTrue(BusinessMembership.objects.filter(user=self.user, business=self.business_a).exists())
        self.assertTrue(CustomerProfile.objects.filter(user=self.user).exists())
        self.assertTrue(
            CustomerBusinessLink.objects.filter(customer_profile__user=self.user, business=self.business_b).exists()
        )

    def test_staff_member_can_also_be_a_customer_of_the_same_business_they_work_at(self):
        """No constraint prevents this — Customer/CustomerProfile and BusinessMembership are independent concepts."""
        from customers.models import CustomerBusinessLink, CustomerProfile

        BusinessMembership.objects.create(
            business=self.business_a, user=self.user, role=BusinessMembership.Role.STAFF
        )
        profile = CustomerProfile.objects.create(user=self.user, full_name="Dana")
        link = CustomerBusinessLink.objects.create(customer_profile=profile, business=self.business_a)

        self.assertTrue(BusinessMembership.objects.filter(user=self.user, business=self.business_a).exists())
        self.assertEqual(link.business_id, self.business_a.id)

    def test_being_staff_does_not_create_a_customer_profile_and_vice_versa(self):
        """No signal/hook implicitly creates one from the other — they're created independently, on purpose."""
        from customers.models import CustomerProfile

        BusinessMembership.objects.create(
            business=self.business_a, user=self.user, role=BusinessMembership.Role.STAFF
        )
        self.assertFalse(CustomerProfile.objects.filter(user=self.user).exists())
