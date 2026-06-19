"""
Employees time-tracking endpoints, tenant-scoped via core.permissions.HasBusinessRole.

Clock-in/out and break actions operate on the caller's own
BusinessMembership for the business in the URL (`request.business_membership`,
set by HasBusinessRole) — there is no way to clock another membership in or
out through this API. They are exposed as explicit POST actions rather than
generic CRUD because they are state transitions, not arbitrary field
writes; all of the actual logic lives in employees/services.py.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business, BusinessMembership
from core.permissions import HasBusinessRole, IsBusinessManager, business_ids_for_user

from . import payroll, services
from .models import (
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
from .serializers import (
    CoordinatesSerializer,
    EmployeeAvailabilitySerializer,
    EmployeeShiftSerializer,
    GeofenceSettingSerializer,
    PayStubGenerateSerializer,
    PayStubSerializer,
    PositionSerializer,
    RecurringScheduleSerializer,
    ShiftStatusSerializer,
    ShiftSwapRequestSerializer,
    ShiftTemplateSerializer,
    SwapApprovalSerializer,
    TimeEntrySerializer,
    TimeOffRequestSerializer,
)


def _is_manager_or_above(request):
    """
    Used by viewsets where staff see only their own rows but manager+ see
    everyone's (availability, shift swaps, time off, pay stubs) — separate
    from core.permissions.HasBusinessRole, which gates whether the request
    is allowed at all, not how much of the queryset it can see.
    """
    if getattr(request.user, "is_superadmin", False):
        return True
    membership = getattr(request, "business_membership", None)
    if membership is None:
        return False
    return BusinessMembership.ROLE_RANK[membership.role] >= BusinessMembership.ROLE_RANK[BusinessMembership.Role.MANAGER]


class GeofenceSettingViewSet(viewsets.ModelViewSet):
    serializer_class = GeofenceSettingSerializer
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business_id = self.kwargs["business_id"]
        business = Business.objects.filter(
            id=business_id, id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return GeofenceSetting.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def perform_create(self, serializer):
        serializer.save(business=self.get_business())


class TimeEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only list/retrieve plus the clock-in/out/break state-transition actions."""

    serializer_class = TimeEntrySerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return TimeEntry.objects.filter(membership__business_id=self.kwargs["business_id"])

    def _own_membership(self) -> BusinessMembership:
        # Set by HasBusinessRole.has_permission for every request that
        # carries a business_id in the URL, which every route on this
        # viewset does.
        return self.request.business_membership

    def _error_response(self, exc: services.TimeTrackingError):
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="clock-in")
    def clock_in(self, request, business_id=None):
        coords = CoordinatesSerializer(data=request.data)
        coords.is_valid(raise_exception=True)
        try:
            entry = services.clock_in(
                self._own_membership(),
                coords.validated_data["latitude"],
                coords.validated_data["longitude"],
            )
        except services.TimeTrackingError as exc:
            return self._error_response(exc)
        return Response(TimeEntrySerializer(entry).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="clock-out")
    def clock_out(self, request, business_id=None):
        coords = CoordinatesSerializer(data=request.data)
        coords.is_valid(raise_exception=True)
        try:
            entry = services.clock_out(
                self._own_membership(),
                coords.validated_data["latitude"],
                coords.validated_data["longitude"],
            )
        except services.TimeTrackingError as exc:
            return self._error_response(exc)
        return Response(TimeEntrySerializer(entry).data)

    @action(detail=False, methods=["post"], url_path="break-start")
    def break_start(self, request, business_id=None):
        try:
            entry_break = services.start_break(self._own_membership())
        except services.TimeTrackingError as exc:
            return self._error_response(exc)
        return Response(TimeEntrySerializer(entry_break.time_entry).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="break-end")
    def break_end(self, request, business_id=None):
        try:
            entry_break = services.end_break(self._own_membership())
        except services.TimeTrackingError as exc:
            return self._error_response(exc)
        return Response(TimeEntrySerializer(entry_break.time_entry).data)


class PositionViewSet(viewsets.ModelViewSet):
    serializer_class = PositionSerializer
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return Position.objects.filter(business_id=self.kwargs["business_id"])

    def perform_create(self, serializer):
        serializer.save(business=self.get_business())


class EmployeeAvailabilityViewSet(viewsets.ModelViewSet):
    """
    Staff manage only their own availability; manager+ can see everyone's
    (for scheduling) but, in this pass, still only create their own — see
    serializer docstring.
    """

    serializer_class = EmployeeAvailabilitySerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        qs = EmployeeAvailability.objects.filter(membership__business_id=self.kwargs["business_id"])
        if _is_manager_or_above(self.request):
            return qs
        return qs.filter(membership=self.request.business_membership)

    def perform_create(self, serializer):
        serializer.save(membership=self.request.business_membership)


class ShiftTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = ShiftTemplateSerializer
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return ShiftTemplate.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def perform_create(self, serializer):
        serializer.save(business=self.get_business())


class RecurringScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = RecurringScheduleSerializer
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return RecurringSchedule.objects.filter(membership__business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context


class EmployeeShiftViewSet(viewsets.ModelViewSet):
    """
    Visible to the whole team (it's a schedule, not sensitive data); only
    manager+ can create/edit shifts or change a shift's status.
    """

    serializer_class = EmployeeShiftSerializer
    business_lookup_url_kwarg = "business_id"

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy", "set_status"):
            return [IsBusinessManager()]
        return [HasBusinessRole()]

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return EmployeeShift.objects.filter(membership__business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, business_id=None, pk=None):
        shift = self.get_object()
        serializer = ShiftStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shift.status = serializer.validated_data["status"]
        shift.save(update_fields=["status", "updated_at"])
        return Response(EmployeeShiftSerializer(shift).data)


class ShiftSwapRequestViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ShiftSwapRequestSerializer
    business_lookup_url_kwarg = "business_id"

    def get_permissions(self):
        if self.action in ("approve", "reject"):
            return [IsBusinessManager()]
        return [HasBusinessRole()]

    def get_queryset(self):
        qs = ShiftSwapRequest.objects.filter(shift__membership__business_id=self.kwargs["business_id"])
        if _is_manager_or_above(self.request):
            return qs
        own = self.request.business_membership
        return qs.filter(Q(requesting_membership=own) | Q(target_membership=own))

    def perform_create(self, serializer):
        shift = serializer.validated_data["shift"]
        try:
            swap_request = services.request_shift_swap(
                shift,
                self.request.business_membership,
                serializer.validated_data.get("target_membership"),
            )
        except services.ShiftSwapError as exc:
            raise serializers.ValidationError({"detail": str(exc)})
        serializer.instance = swap_request

    @action(detail=True, methods=["post"])
    def approve(self, request, business_id=None, pk=None):
        swap_request = self.get_object()
        input_serializer = SwapApprovalSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        target_membership = None
        target_id = input_serializer.validated_data.get("target_membership_id")
        if target_id:
            target_membership = get_object_or_404(BusinessMembership, id=target_id, business_id=business_id)
        try:
            swap_request = services.approve_shift_swap(swap_request, request.business_membership, target_membership)
        except services.ShiftSwapError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ShiftSwapRequestSerializer(swap_request).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, business_id=None, pk=None):
        swap_request = self.get_object()
        try:
            swap_request = services.reject_shift_swap(swap_request, request.business_membership)
        except services.ShiftSwapError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ShiftSwapRequestSerializer(swap_request).data)


class TimeOffRequestViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = TimeOffRequestSerializer
    business_lookup_url_kwarg = "business_id"

    def get_permissions(self):
        if self.action in ("approve", "reject"):
            return [IsBusinessManager()]
        return [HasBusinessRole()]

    def get_queryset(self):
        qs = TimeOffRequest.objects.filter(membership__business_id=self.kwargs["business_id"])
        if _is_manager_or_above(self.request):
            return qs
        return qs.filter(membership=self.request.business_membership)

    def perform_create(self, serializer):
        serializer.save(membership=self.request.business_membership)

    @action(detail=True, methods=["post"])
    def approve(self, request, business_id=None, pk=None):
        time_off_request = self.get_object()
        try:
            time_off_request = services.approve_time_off(time_off_request, request.business_membership)
        except services.TimeOffError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TimeOffRequestSerializer(time_off_request).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, business_id=None, pk=None):
        time_off_request = self.get_object()
        try:
            time_off_request = services.reject_time_off(time_off_request, request.business_membership)
        except services.TimeOffError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TimeOffRequestSerializer(time_off_request).data)


class PayStubViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PayStubSerializer
    business_lookup_url_kwarg = "business_id"

    def get_permissions(self):
        if self.action == "generate":
            return [IsBusinessManager()]
        return [HasBusinessRole()]

    def get_queryset(self):
        qs = PayStub.objects.filter(membership__business_id=self.kwargs["business_id"])
        if _is_manager_or_above(self.request):
            return qs
        return qs.filter(membership=self.request.business_membership)

    @action(detail=False, methods=["post"])
    def generate(self, request, business_id=None):
        input_serializer = PayStubGenerateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        membership = get_object_or_404(BusinessMembership, id=data["membership_id"], business_id=business_id)
        position = get_object_or_404(Position, id=data["position_id"], business_id=business_id)

        try:
            pay_stub = payroll.generate_pay_stub(
                membership, position, data["pay_period_start"], data["pay_period_end"]
            )
        except payroll.PayStubAlreadyExistsError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PayStubSerializer(pay_stub).data, status=status.HTTP_201_CREATED)
