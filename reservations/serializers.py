"""
Reservations serializers.

Staff-side URLs are business-scoped, not location-scoped
(`/api/businesses/<business_id>/tables/`, same convention as every other
domain), but most models here belong to one of potentially several
`BusinessLocation`s under that business — so unlike `Customer.business`
(which the view can infer entirely from the URL), `location` can't be
inferred and has to be a normal writable field, validated against the URL's
business via `self.context["business"]` (same pattern as
`employees.serializers.GeofenceSettingSerializer.validate_location`).

Guest-facing serializers (`GuestReservationSerializer`,
`GuestWaitlistSerializer`) have no `location` field at all — the view
resolves it from the URL and passes it straight to the booking service,
never accepting it from the request body. That matters even more here than
on the staff side because these endpoints are reachable with no
authentication at all (see `reservations/public_views.py`); a client could
otherwise smuggle a `location` id for a business it has no relationship to
into a guest booking payload.
"""

import uuid
from datetime import timedelta

from django.core.validators import RegexValidator
from rest_framework import serializers

from . import services
from .models import (
    BlackoutDate,
    BusinessHours,
    FloorPlan,
    Reservation,
    ReservationSetting,
    RestaurantTable,
    Waitlist,
)

# Same pattern as customers/serializers.py phone_validator (Phase 1 audit
# finding: phone/email format must be enforced server-side, not just
# client-side) — duplicated rather than imported since each domain app
# stays self-contained.
phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)

# Keys that must never appear inside a per-table entry of FloorPlan.layout —
# position lives only on RestaurantTable (see models.py module docstring,
# fix #3). Allowing these back in here is exactly how the JSONB/column drift
# happened in the old system.
_FORBIDDEN_TABLE_LAYOUT_KEYS = {"x", "y", "position_x", "position_y", "position"}


class RestaurantTableSerializer(serializers.ModelSerializer):
    class Meta:
        model = RestaurantTable
        fields = [
            "id",
            "location",
            "name",
            "capacity",
            "position_x",
            "position_y",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location


class FloorPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = FloorPlan
        fields = ["id", "location", "name", "layout", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location

    def validate(self, attrs):
        layout = attrs.get("layout", getattr(self.instance, "layout", {}))
        location = attrs.get("location", getattr(self.instance, "location", None))
        attrs["layout"] = self._validate_layout(layout, location)
        return attrs

    def _validate_layout(self, layout, location):
        if not isinstance(layout, dict):
            raise serializers.ValidationError({"layout": "Must be a JSON object."})

        tables = layout.get("tables", [])
        if not isinstance(tables, list):
            raise serializers.ValidationError({"layout": {"tables": "Must be a list."}})

        table_ids = []
        for entry in tables:
            if not isinstance(entry, dict) or "table_id" not in entry:
                raise serializers.ValidationError(
                    {"layout": {"tables": 'Each entry must be an object with a "table_id".'}}
                )
            forbidden = _FORBIDDEN_TABLE_LAYOUT_KEYS & entry.keys()
            if forbidden:
                raise serializers.ValidationError(
                    {
                        "layout": {
                            "tables": (
                                f"Entry for table_id={entry['table_id']!r} may not carry position data "
                                f"({', '.join(sorted(forbidden))}) — position lives only on RestaurantTable."
                            )
                        }
                    }
                )
            try:
                table_id = uuid.UUID(str(entry["table_id"]))
            except (ValueError, TypeError):
                raise serializers.ValidationError({"layout": {"tables": f"Invalid table_id: {entry['table_id']!r}."}})
            table_ids.append(table_id)

        if len(table_ids) != len(set(table_ids)):
            raise serializers.ValidationError({"layout": {"tables": "table_id values must be unique."}})

        if table_ids and location is not None:
            existing = set(
                RestaurantTable.objects.filter(location=location, id__in=table_ids).values_list("id", flat=True)
            )
            missing = set(table_ids) - existing
            if missing:
                raise serializers.ValidationError(
                    {
                        "layout": {
                            "tables": f"Unknown table_id(s) for this location: {', '.join(str(t) for t in missing)}."
                        }
                    }
                )

        return layout


class BusinessHoursSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessHours
        fields = [
            "id",
            "location",
            "day_of_week",
            "open_time",
            "close_time",
            "is_closed",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location

    def validate(self, attrs):
        is_closed = attrs.get("is_closed", getattr(self.instance, "is_closed", False))
        open_time = attrs.get("open_time", getattr(self.instance, "open_time", None))
        close_time = attrs.get("close_time", getattr(self.instance, "close_time", None))
        if not is_closed:
            if not open_time or not close_time:
                raise serializers.ValidationError("open_time and close_time are required unless is_closed.")
            if close_time <= open_time:
                raise serializers.ValidationError({"close_time": "Must be after open_time."})
        return attrs


class BlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlackoutDate
        fields = ["id", "location", "date", "reason", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location


class ReservationSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReservationSetting
        fields = [
            "id",
            "business",
            "default_duration_minutes",
            "slot_interval_minutes",
            "buffer_minutes",
            "min_advance_minutes",
            "max_advance_days",
            "max_party_size",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]


class ReservationSerializer(serializers.ModelSerializer):
    """Staff-side: full CRUD, including the guest's own contact details and internal notes."""

    guest_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    guest_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Reservation
        fields = [
            "id",
            "location",
            "table",
            "guest_name",
            "guest_email",
            "guest_phone",
            "party_size",
            "start_time",
            "end_time",
            "duration_minutes",
            "status",
            "confirmation_code",
            "notes",
            "created_at",
            "updated_at",
        ]
        # `end_time` and `confirmation_code` are always computed/generated
        # by Reservation.save() (see models.py) — never client input,
        # staff or guest. `status` only changes via the seat/cancel/no-show
        # actions.
        read_only_fields = [
            "id",
            "end_time",
            "status",
            "confirmation_code",
            "created_at",
            "updated_at",
        ]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location

    def validate(self, attrs):
        location = attrs.get("location", getattr(self.instance, "location", None))
        table = attrs.get("table", getattr(self.instance, "table", None))
        if table is not None and location is not None and table.location_id != location.id:
            raise serializers.ValidationError({"table": "Table does not belong to this location."})

        # Best-effort overlap guard for direct staff CRUD (no select_for_update
        # here — this path isn't the high-volume target of the Phase 1 audit
        # finding, which is the public booking flow; that one is
        # lock-protected end-to-end in services.book_reservation). Still
        # worth catching the obvious case rather than silently double-booking.
        if table is not None:
            start_time = attrs.get("start_time", getattr(self.instance, "start_time", None))
            end_time = attrs.get("end_time", getattr(self.instance, "end_time", None))
            if start_time and not end_time:
                duration = attrs.get("duration_minutes", getattr(self.instance, "duration_minutes", None))
                duration = duration or services.get_settings(location.business).default_duration_minutes
                end_time = start_time + timedelta(minutes=duration)
            if start_time and end_time:
                qs = Reservation.objects.filter(
                    table=table,
                    status__in=Reservation.ACTIVE_STATUSES,
                    start_time__lt=end_time,
                    end_time__gt=start_time,
                )
                if self.instance is not None:
                    qs = qs.exclude(pk=self.instance.pk)
                if qs.exists():
                    raise serializers.ValidationError({"table": "This table is already booked for an overlapping time."})
        return attrs


class GuestReservationSerializer(serializers.ModelSerializer):
    """
    Public booking input/output. No `location`/`table`/`status`/`notes` —
    location comes from the URL (see public_views.py), table is assigned by
    the booking service, status is managed server-side, and internal staff
    notes are never guest-visible.
    """

    guest_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    guest_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Reservation
        fields = [
            "id",
            "guest_name",
            "guest_email",
            "guest_phone",
            "party_size",
            "start_time",
            "end_time",
            "duration_minutes",
            "status",
            "confirmation_code",
        ]
        read_only_fields = ["id", "end_time", "status", "confirmation_code"]


class WaitlistSerializer(serializers.ModelSerializer):
    """Staff-side waitlist CRUD."""

    guest_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    guest_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Waitlist
        fields = [
            "id",
            "location",
            "guest_name",
            "guest_email",
            "guest_phone",
            "party_size",
            "requested_time",
            "status",
            "reservation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "reservation", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if business and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location


class GuestWaitlistSerializer(serializers.ModelSerializer):
    """Public join-the-waitlist input/output — same exclusions as GuestReservationSerializer."""

    guest_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    guest_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Waitlist
        fields = ["id", "guest_name", "guest_email", "guest_phone", "party_size", "requested_time", "status"]
        read_only_fields = ["id", "status"]


class WaitlistConvertSerializer(serializers.Serializer):
    """Optional overrides when staff convert a waiting guest into a real Reservation."""

    start_time = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(min_value=1, required=False)


class AvailabilityQuerySerializer(serializers.Serializer):
    """Guest-facing availability check input (query params)."""

    date = serializers.DateField()
    party_size = serializers.IntegerField(min_value=1)
    duration_minutes = serializers.IntegerField(min_value=1, required=False)
