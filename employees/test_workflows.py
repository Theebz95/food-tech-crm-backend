from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership

from .models import EmployeeShift, Position, ShiftSwapRequest, TimeOffRequest


def positions_url(business_id):
    return f"/api/businesses/{business_id}/positions/"


def shifts_url(business_id):
    return f"/api/businesses/{business_id}/shifts/"


def shift_set_status_url(business_id, shift_id):
    return f"/api/businesses/{business_id}/shifts/{shift_id}/set-status/"


def swap_requests_url(business_id):
    return f"/api/businesses/{business_id}/shift-swap-requests/"


def swap_approve_url(business_id, swap_id):
    return f"/api/businesses/{business_id}/shift-swap-requests/{swap_id}/approve/"


def swap_reject_url(business_id, swap_id):
    return f"/api/businesses/{business_id}/shift-swap-requests/{swap_id}/reject/"


def time_off_url(business_id):
    return f"/api/businesses/{business_id}/time-off-requests/"


def time_off_approve_url(business_id, request_id):
    return f"/api/businesses/{business_id}/time-off-requests/{request_id}/approve/"


def time_off_reject_url(business_id, request_id):
    return f"/api/businesses/{business_id}/time-off-requests/{request_id}/reject/"


def pay_stub_generate_url(business_id):
    return f"/api/businesses/{business_id}/pay-stubs/generate/"


def pay_stubs_url(business_id):
    return f"/api/businesses/{business_id}/pay-stubs/"


class ShiftSwapFlowTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner-swap@example.com")
        self.business = Business.objects.create(name="Swap Co", owner=owner)
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))

        self.staff1_user = User.objects.create_user(email="staff1-swap@example.com")
        self.staff1 = BusinessMembership.objects.create(
            business=self.business, user=self.staff1_user, role=BusinessMembership.Role.STAFF
        )
        self.staff2_user = User.objects.create_user(email="staff2-swap@example.com")
        self.staff2 = BusinessMembership.objects.create(
            business=self.business, user=self.staff2_user, role=BusinessMembership.Role.STAFF
        )
        self.manager_user = User.objects.create_user(email="manager-swap@example.com")
        self.manager = BusinessMembership.objects.create(
            business=self.business, user=self.manager_user, role=BusinessMembership.Role.MANAGER
        )

        start = timezone.now() + timedelta(days=1)
        self.shift = EmployeeShift.objects.create(
            membership=self.staff1, position=self.position, start_at=start, end_at=start + timedelta(hours=8)
        )
        self.client = APIClient()

    def test_request_approve_reassigns_shift(self):
        self.client.force_authenticate(user=self.staff1_user)
        create_response = self.client.post(
            swap_requests_url(self.business.id), {"shift": str(self.shift.id), "target_membership": str(self.staff2.id)}
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        swap_id = create_response.data["id"]

        self.client.force_authenticate(user=self.manager_user)
        approve_response = self.client.post(swap_approve_url(self.business.id, swap_id))
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK, approve_response.data)
        self.assertEqual(approve_response.data["status"], ShiftSwapRequest.Status.APPROVED)

        self.shift.refresh_from_db()
        self.assertEqual(self.shift.membership_id, self.staff2.id)

    def test_staff_cannot_request_swap_for_someone_elses_shift(self):
        self.client.force_authenticate(user=self.staff2_user)
        response = self.client.post(
            swap_requests_url(self.business.id), {"shift": str(self.shift.id), "target_membership": str(self.staff1.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_cannot_approve_swap(self):
        self.client.force_authenticate(user=self.staff1_user)
        create_response = self.client.post(
            swap_requests_url(self.business.id), {"shift": str(self.shift.id), "target_membership": str(self.staff2.id)}
        )
        swap_id = create_response.data["id"]

        response = self.client.post(swap_approve_url(self.business.id, swap_id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_resolve_open_request_on_approval(self):
        self.client.force_authenticate(user=self.staff1_user)
        create_response = self.client.post(swap_requests_url(self.business.id), {"shift": str(self.shift.id)})
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        swap_id = create_response.data["id"]
        self.assertIsNone(create_response.data["target_membership"])

        self.client.force_authenticate(user=self.manager_user)
        approve_response = self.client.post(
            swap_approve_url(self.business.id, swap_id), {"target_membership_id": str(self.staff2.id)}
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK, approve_response.data)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.membership_id, self.staff2.id)

    def test_reject_does_not_reassign_shift(self):
        self.client.force_authenticate(user=self.staff1_user)
        create_response = self.client.post(
            swap_requests_url(self.business.id), {"shift": str(self.shift.id), "target_membership": str(self.staff2.id)}
        )
        swap_id = create_response.data["id"]

        self.client.force_authenticate(user=self.manager_user)
        reject_response = self.client.post(swap_reject_url(self.business.id, swap_id))
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)
        self.assertEqual(reject_response.data["status"], ShiftSwapRequest.Status.REJECTED)

        self.shift.refresh_from_db()
        self.assertEqual(self.shift.membership_id, self.staff1.id)


class TimeOffFlowTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner-timeoff@example.com")
        self.business = Business.objects.create(name="TimeOff Co", owner=owner)
        self.staff_user = User.objects.create_user(email="staff-timeoff@example.com")
        self.staff = BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.manager_user = User.objects.create_user(email="manager-timeoff@example.com")
        self.manager = BusinessMembership.objects.create(
            business=self.business, user=self.manager_user, role=BusinessMembership.Role.MANAGER
        )
        self.client = APIClient()

    def test_request_then_approve(self):
        self.client.force_authenticate(user=self.staff_user)
        today = timezone.now().date()
        create_response = self.client.post(
            time_off_url(self.business.id),
            {"start_date": str(today), "end_date": str(today + timedelta(days=2)), "reason": "Vacation"},
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        request_id = create_response.data["id"]
        self.assertEqual(create_response.data["status"], TimeOffRequest.Status.PENDING)

        self.client.force_authenticate(user=self.manager_user)
        approve_response = self.client.post(time_off_approve_url(self.business.id, request_id))
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(approve_response.data["status"], TimeOffRequest.Status.APPROVED)
        self.assertEqual(approve_response.data["approved_by"], self.manager.id)

    def test_request_then_reject(self):
        self.client.force_authenticate(user=self.staff_user)
        today = timezone.now().date()
        create_response = self.client.post(
            time_off_url(self.business.id), {"start_date": str(today), "end_date": str(today)}
        )
        request_id = create_response.data["id"]

        self.client.force_authenticate(user=self.manager_user)
        reject_response = self.client.post(time_off_reject_url(self.business.id, request_id))
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)
        self.assertEqual(reject_response.data["status"], TimeOffRequest.Status.REJECTED)

    def test_staff_cannot_approve_own_request(self):
        self.client.force_authenticate(user=self.staff_user)
        today = timezone.now().date()
        create_response = self.client.post(
            time_off_url(self.business.id), {"start_date": str(today), "end_date": str(today)}
        )
        request_id = create_response.data["id"]

        response = self.client.post(time_off_approve_url(self.business.id, request_id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_end_date_before_start_date_rejected(self):
        self.client.force_authenticate(user=self.staff_user)
        today = timezone.now().date()
        response = self.client.post(
            time_off_url(self.business.id),
            {"start_date": str(today), "end_date": str(today - timedelta(days=1))},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ManagerOnlyGatingTests(TestCase):
    """Write/approval actions across the new models must reject plain staff."""

    def setUp(self):
        owner = User.objects.create_user(email="owner-gating@example.com")
        self.business = Business.objects.create(name="Gating Co", owner=owner)
        self.position = Position.objects.create(business=self.business, name="Server", hourly_rate=Decimal("20.00"))
        self.staff_user = User.objects.create_user(email="staff-gating@example.com")
        self.staff = BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_staff_cannot_create_position(self):
        response = self.client.post(positions_url(self.business.id), {"name": "Manager", "hourly_rate": "30.00"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_create_shift(self):
        start = timezone.now() + timedelta(days=1)
        response = self.client.post(
            shifts_url(self.business.id),
            {
                "membership": str(self.staff.id),
                "position": str(self.position.id),
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=8)).isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_set_shift_status(self):
        start = timezone.now() + timedelta(days=1)
        shift = EmployeeShift.objects.create(
            membership=self.staff, position=self.position, start_at=start, end_at=start + timedelta(hours=8)
        )
        response = self.client.post(
            shift_set_status_url(self.business.id, shift.id), {"status": EmployeeShift.Status.COMPLETED}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_generate_pay_stub(self):
        today = timezone.now().date()
        response = self.client.post(
            pay_stub_generate_url(self.business.id),
            {
                "membership_id": str(self.staff.id),
                "position_id": str(self.position.id),
                "pay_period_start": str(today - timedelta(days=7)),
                "pay_period_end": str(today),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TenantIsolationAcrossSchedulingModelsTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner-iso@example.com")
        self.business_a = Business.objects.create(name="Iso A", owner=owner)
        self.business_b = Business.objects.create(name="Iso B", owner=owner)
        self.position_b = Position.objects.create(business=self.business_b, name="Server", hourly_rate=Decimal("20.00"))

        self.user_a = User.objects.create_user(email="staffa-iso@example.com")
        BusinessMembership.objects.create(business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF)

        manager_b_user = User.objects.create_user(email="managerb-iso@example.com")
        self.manager_b = BusinessMembership.objects.create(
            business=self.business_b, user=manager_b_user, role=BusinessMembership.Role.MANAGER
        )
        start = timezone.now() + timedelta(days=1)
        self.shift_b = EmployeeShift.objects.create(
            membership=self.manager_b, position=self.position_b, start_at=start, end_at=start + timedelta(hours=8)
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_access_other_business_endpoints(self):
        urls = [
            positions_url(self.business_b.id),
            shifts_url(self.business_b.id),
            swap_requests_url(self.business_b.id),
            time_off_url(self.business_b.id),
            pay_stubs_url(self.business_b.id),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, url)

    def test_cannot_create_in_other_business(self):
        response = self.client.post(
            positions_url(self.business_b.id), {"name": "Hacker Position", "hourly_rate": "999.00"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Position.objects.filter(name="Hacker Position").exists())
