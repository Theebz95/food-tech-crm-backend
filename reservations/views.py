"""
Staff-side Reservations endpoints, tenant-scoped via core.permissions.HasBusinessRole
— same pattern as every other domain. See reservations/public_views.py for
the guest-facing endpoints, which intentionally do NOT use HasBusinessRole
at all (there's no User/BusinessMembership for an unauthenticated guest).
"""

from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import services
from .models import BlackoutDate, BusinessHours, FloorPlan, Reservation, ReservationSetting, RestaurantTable, Waitlist
from .serializers import (
    BlackoutDateSerializer,
    BusinessHoursSerializer,
    FloorPlanSerializer,
    ReservationSerializer,
    ReservationSettingSerializer,
    RestaurantTableSerializer,
    WaitlistConvertSerializer,
    WaitlistSerializer,
)


class _BusinessScopedViewSet(viewsets.ModelViewSet):
    """Shared get_business()/context plumbing for every business-scoped staff ViewSet below."""

    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context


class RestaurantTableViewSet(_BusinessScopedViewSet):
    serializer_class = RestaurantTableSerializer

    def get_queryset(self):
        return RestaurantTable.objects.filter(location__business_id=self.kwargs["business_id"])


class FloorPlanViewSet(_BusinessScopedViewSet):
    serializer_class = FloorPlanSerializer

    def get_queryset(self):
        return FloorPlan.objects.filter(location__business_id=self.kwargs["business_id"])


class BusinessHoursViewSet(_BusinessScopedViewSet):
    serializer_class = BusinessHoursSerializer

    def get_queryset(self):
        return BusinessHours.objects.filter(location__business_id=self.kwargs["business_id"])


class BlackoutDateViewSet(_BusinessScopedViewSet):
    serializer_class = BlackoutDateSerializer

    def get_queryset(self):
        return BlackoutDate.objects.filter(location__business_id=self.kwargs["business_id"])


class ReservationSettingView(generics.RetrieveUpdateAPIView):
    """
    One settings row per business (`ReservationSetting.business` is a
    OneToOneField) — GET/PUT/PATCH only, auto-creating it with defaults on
    first access rather than requiring a separate "create" step. There is
    deliberately no list/create/delete here.
    """

    serializer_class = ReservationSettingSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_object(self):
        setting, _ = ReservationSetting.objects.get_or_create(business=self.get_business())
        self.check_object_permissions(self.request, setting)
        return setting


class ReservationViewSet(_BusinessScopedViewSet):
    serializer_class = ReservationSerializer

    def get_queryset(self):
        return Reservation.objects.filter(location__business_id=self.kwargs["business_id"])

    def _transition(self, reservation, allowed_from, new_status, error_detail):
        if reservation.status not in allowed_from:
            return Response({"detail": error_detail}, status=status.HTTP_400_BAD_REQUEST)
        reservation.status = new_status
        reservation.save(update_fields=["status", "updated_at"])
        return Response(ReservationSerializer(reservation).data)

    @action(detail=True, methods=["post"])
    def seat(self, request, business_id=None, pk=None):
        reservation = self.get_object()
        return self._transition(
            reservation,
            (Reservation.Status.PENDING, Reservation.Status.CONFIRMED),
            Reservation.Status.SEATED,
            "Only a pending or confirmed reservation can be seated.",
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, business_id=None, pk=None):
        reservation = self.get_object()
        return self._transition(
            reservation,
            (Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.SEATED),
            Reservation.Status.CANCELLED,
            f"Cannot cancel a reservation that is already {reservation.status}.",
        )

    @action(detail=True, methods=["post"], url_path="no-show")
    def no_show(self, request, business_id=None, pk=None):
        reservation = self.get_object()
        return self._transition(
            reservation,
            (Reservation.Status.PENDING, Reservation.Status.CONFIRMED),
            Reservation.Status.NO_SHOW,
            "Only a pending or confirmed reservation can be marked no-show.",
        )

    @action(detail=True, methods=["post"])
    def complete(self, request, business_id=None, pk=None):
        reservation = self.get_object()
        return self._transition(
            reservation,
            (Reservation.Status.SEATED,),
            Reservation.Status.COMPLETED,
            "Only a seated reservation can be completed.",
        )


class WaitlistViewSet(_BusinessScopedViewSet):
    serializer_class = WaitlistSerializer

    def get_queryset(self):
        return Waitlist.objects.filter(location__business_id=self.kwargs["business_id"])

    @action(detail=True, methods=["post"], url_path="convert-to-reservation")
    def convert_to_reservation(self, request, business_id=None, pk=None):
        entry = self.get_object()
        input_serializer = WaitlistConvertSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        try:
            reservation = services.convert_waitlist_entry(
                entry,
                start_time=input_serializer.validated_data.get("start_time"),
                duration_minutes=input_serializer.validated_data.get("duration_minutes"),
            )
        except services.BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)
