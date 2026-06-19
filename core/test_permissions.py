"""
Direct unit tests for HasBusinessRole.has_object_permission.

Background: several employees models (TimeEntry, EmployeeAvailability,
RecurringSchedule, EmployeeShift, ShiftSwapRequest, TimeOffRequest,
PayStub) only have a `membership` FK, not a direct `business` field.
has_object_permission's business resolution (`obj.business_id` /
`obj.business.id`) silently returned False for these — denying everyone,
including legitimate same-business managers — until each model got a
`business` property (see employees/models.py).

Why these are unit tests on the permission class directly, not just
HTTP-level tests through a view: at the view layer, HasBusinessRole.has_permission
(the URL-kwarg check, run before get_object() is ever called) already
denies a user with no membership on the URL's business — and every
viewset's get_queryset() already scopes objects to that business. So an
object belonging to a *different* business than the URL claims can never
even be fetched to reach has_object_permission in the first place; an
HTTP-level "wrong business" test would actually be exercising
has_permission + queryset scoping, not this method. These tests call
has_object_permission directly, bypassing the view entirely, so the
method's own deny behavior is verified in isolation.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from authentication.models import User
from customers.models import Customer
from employees.models import (
    EmployeeAvailability,
    EmployeeShift,
    GeofenceSetting,
    PayStub,
    Position,
    RecurringSchedule,
    ShiftSwapRequest,
    ShiftTemplate,
    TimeEntry,
    TimeOffRequest,
)

from .models import Business, BusinessMembership
from .permissions import HasBusinessRole, IsBusinessManager


class FakeRequest:
    """has_object_permission only reads request.user — no need for a real DRF Request."""

    def __init__(self, user):
        self.user = user


class HasObjectPermissionTests(TestCase):
    """
    Same object (belonging to Business B), two different requesting users:
    one who is a manager of B (must be allowed) and one who is a manager of
    a *different* business, A, with no membership on B at all (must be
    denied). Run for every model type that's reachable via a detail route
    in either the Customers or Employees domain.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner-objperm@example.com")
        self.business_a = Business.objects.create(name="ObjPerm A", owner=owner)
        self.business_b = Business.objects.create(name="ObjPerm B", owner=owner)

        # Manager of B — the legitimate user for every "allowed" assertion below.
        manager_user = User.objects.create_user(email="manager-objperm@example.com")
        self.membership_b = BusinessMembership.objects.create(
            business=self.business_b, user=manager_user, role=BusinessMembership.Role.MANAGER
        )
        self.allowed_request = FakeRequest(manager_user)

        # Manager of A only — has a real, sufficiently-privileged membership,
        # just on the wrong business. This is "a user with no membership on
        # the resource's business" from the bug report, made concrete.
        outsider_user = User.objects.create_user(email="outsider-objperm@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=outsider_user, role=BusinessMembership.Role.MANAGER
        )
        self.denied_request = FakeRequest(outsider_user)

        self.position_b = Position.objects.create(business=self.business_b, name="Server", hourly_rate=Decimal("20.00"))

    def assertAllowedAndDenied(self, permission_class, obj):
        permission = permission_class()
        self.assertTrue(
            permission.has_object_permission(self.allowed_request, None, obj),
            f"Manager of the owning business was denied access to {obj!r}.",
        )
        self.assertFalse(
            permission.has_object_permission(self.denied_request, None, obj),
            f"Manager of an unrelated business was allowed access to {obj!r}.",
        )

    def test_customer(self):
        customer = Customer.objects.create(business=self.business_b, name="Someone")
        self.assertAllowedAndDenied(HasBusinessRole, customer)

    def test_geofence_setting(self):
        setting = GeofenceSetting.objects.create(
            business=self.business_b,
            center_latitude=Decimal("1.000000"),
            center_longitude=Decimal("1.000000"),
            radius_meters=100,
        )
        self.assertAllowedAndDenied(IsBusinessManager, setting)

    def test_position(self):
        self.assertAllowedAndDenied(IsBusinessManager, self.position_b)

    def test_time_entry(self):
        entry = TimeEntry.objects.create(membership=self.membership_b, clock_in_at=timezone.now())
        self.assertAllowedAndDenied(HasBusinessRole, entry)

    def test_employee_availability(self):
        availability = EmployeeAvailability.objects.create(
            membership=self.membership_b, day_of_week=0, start_time="09:00", end_time="17:00"
        )
        self.assertAllowedAndDenied(HasBusinessRole, availability)

    def test_shift_template(self):
        template = ShiftTemplate.objects.create(
            business=self.business_b, position=self.position_b, day_of_week=0, start_time="09:00", end_time="17:00"
        )
        self.assertAllowedAndDenied(IsBusinessManager, template)

    def test_recurring_schedule(self):
        template = ShiftTemplate.objects.create(
            business=self.business_b, position=self.position_b, day_of_week=0, start_time="09:00", end_time="17:00"
        )
        schedule = RecurringSchedule.objects.create(
            membership=self.membership_b,
            shift_template=template,
            recurrence_rule=RecurringSchedule.Recurrence.WEEKLY,
            start_date=timezone.now().date(),
        )
        self.assertAllowedAndDenied(IsBusinessManager, schedule)

    def test_employee_shift(self):
        start = timezone.now()
        shift = EmployeeShift.objects.create(
            membership=self.membership_b, position=self.position_b, start_at=start, end_at=start + timedelta(hours=8)
        )
        self.assertAllowedAndDenied(HasBusinessRole, shift)

    def test_shift_swap_request(self):
        start = timezone.now()
        shift = EmployeeShift.objects.create(
            membership=self.membership_b, position=self.position_b, start_at=start, end_at=start + timedelta(hours=8)
        )
        swap = ShiftSwapRequest.objects.create(shift=shift, requesting_membership=self.membership_b)
        self.assertAllowedAndDenied(HasBusinessRole, swap)

    def test_time_off_request(self):
        today = timezone.now().date()
        time_off = TimeOffRequest.objects.create(membership=self.membership_b, start_date=today, end_date=today)
        self.assertAllowedAndDenied(HasBusinessRole, time_off)

    def test_pay_stub(self):
        today = timezone.now().date()
        pay_stub = PayStub.objects.create(
            membership=self.membership_b,
            position=self.position_b,
            pay_period_start=today,
            pay_period_end=today,
            regular_hours=Decimal("0"),
            overtime_hours=Decimal("0"),
            gross_pay=Decimal("0"),
            net_pay=Decimal("0"),
            breakdown={},
        )
        self.assertAllowedAndDenied(HasBusinessRole, pay_stub)
