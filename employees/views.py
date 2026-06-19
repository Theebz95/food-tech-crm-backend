"""
Employees time-tracking endpoints, tenant-scoped via core.permissions.HasBusinessRole.

Clock-in/out and break actions operate on the caller's own
BusinessMembership for the business in the URL (`request.business_membership`,
set by HasBusinessRole) — there is no way to clock another membership in or
out through this API. They are exposed as explicit POST actions rather than
generic CRUD because they are state transitions, not arbitrary field
writes; all of the actual logic lives in employees/services.py.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business, BusinessMembership
from core.permissions import HasBusinessRole, IsBusinessManager, business_ids_for_user

from . import services
from .models import GeofenceSetting, TimeEntry
from .serializers import CoordinatesSerializer, GeofenceSettingSerializer, TimeEntrySerializer


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
