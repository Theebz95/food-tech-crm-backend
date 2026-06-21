from decimal import Decimal

from rest_framework import serializers

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
    TimeEntryBreak,
    TimeOffRequest,
)


class GeofenceSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeofenceSetting
        fields = [
            "id",
            "business",
            "location",
            "center_latitude",
            "center_longitude",
            "radius_meters",
            "enabled",
            "created_at",
            "updated_at",
        ]
        # `business` is set by the view from the URL, same pattern as
        # CustomerSerializer — never accepted from the request body.
        read_only_fields = ["id", "business", "created_at", "updated_at"]
        # DRF's unique-together machinery sees the model's
        # UniqueConstraint(fields=["business", "location"]) and, as a side
        # effect, forces `location` to required=True — even though
        # `business` is read_only here so no UniqueTogetherValidator
        # actually gets attached (nothing is gained). Left alone, this
        # makes the model's documented null-location "applies
        # business-wide" case (see GeofenceSetting's docstring)
        # unreachable through the API. Explicitly restored to match the
        # model's own null=True/blank=True.
        extra_kwargs = {"location": {"required": False}}

    def validate_location(self, location):
        business = self.context.get("business")
        if location is not None and business is not None and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location


class TimeEntryBreakSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeEntryBreak
        fields = ["id", "break_start_at", "break_end_at"]
        read_only_fields = fields


class TimeEntrySerializer(serializers.ModelSerializer):
    breaks = TimeEntryBreakSerializer(many=True, read_only=True)

    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "membership",
            "clock_in_at",
            "clock_out_at",
            "status",
            "clock_in_lat",
            "clock_in_lng",
            "clock_out_lat",
            "clock_out_lng",
            "clock_in_distance_meters",
            "clock_out_distance_meters",
            "clock_in_within_geofence",
            "clock_out_within_geofence",
            "breaks",
        ]
        # Entirely read-only: every field here is either set by the
        # clock-in/out/break service functions or computed server-side.
        # There is no generic create/update for TimeEntry — see views.py.
        read_only_fields = fields


class CoordinatesSerializer(serializers.Serializer):
    """Input validation for clock-in/clock-out — raw coordinates only, never a distance or verdict."""

    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=Decimal("-90"), max_value=Decimal("90")
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=Decimal("-180"), max_value=Decimal("180")
    )


class PositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Position
        fields = ["id", "business", "name", "hourly_rate", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "business", "created_at", "updated_at"]


class EmployeeAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeAvailability
        fields = ["id", "membership", "day_of_week", "start_time", "end_time", "created_at", "updated_at"]
        # `membership` is always the caller's own — see EmployeeAvailabilityViewSet.
        read_only_fields = ["id", "membership", "created_at", "updated_at"]

    def validate(self, attrs):
        start_time = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end_time = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError({"end_time": "Must be after start_time."})
        return attrs


class ShiftTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftTemplate
        fields = [
            "id",
            "business",
            "location",
            "position",
            "name",
            "day_of_week",
            "start_time",
            "end_time",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate(self, attrs):
        business = self.context.get("business") or getattr(self.instance, "business", None)
        location = attrs.get("location", getattr(self.instance, "location", None))
        position = attrs.get("position", getattr(self.instance, "position", None))
        if business and location and location.business_id != business.id:
            raise serializers.ValidationError({"location": "Does not belong to this business."})
        if business and position and position.business_id != business.id:
            raise serializers.ValidationError({"position": "Does not belong to this business."})
        return attrs


class RecurringScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecurringSchedule
        fields = [
            "id",
            "membership",
            "shift_template",
            "recurrence_rule",
            "start_date",
            "end_date",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        business = self.context.get("business")
        membership = attrs.get("membership", getattr(self.instance, "membership", None))
        shift_template = attrs.get("shift_template", getattr(self.instance, "shift_template", None))
        if business and membership and membership.business_id != business.id:
            raise serializers.ValidationError({"membership": "Does not belong to this business."})
        if business and shift_template and shift_template.business_id != business.id:
            raise serializers.ValidationError({"shift_template": "Does not belong to this business."})
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        if end_date and start_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        return attrs


class EmployeeShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeShift
        fields = [
            "id",
            "membership",
            "position",
            "recurring_schedule",
            "start_at",
            "end_at",
            "status",
            "created_at",
            "updated_at",
        ]
        # `status` only changes via the set-status action; `recurring_schedule`
        # is only ever set by the expansion task — neither is writable here.
        read_only_fields = ["id", "recurring_schedule", "status", "created_at", "updated_at"]

    def validate(self, attrs):
        business = self.context.get("business")
        membership = attrs.get("membership", getattr(self.instance, "membership", None))
        position = attrs.get("position", getattr(self.instance, "position", None))
        if business and membership and membership.business_id != business.id:
            raise serializers.ValidationError({"membership": "Does not belong to this business."})
        if business and position and position.business_id != business.id:
            raise serializers.ValidationError({"position": "Does not belong to this business."})
        end_at = attrs.get("end_at", getattr(self.instance, "end_at", None))
        start_at = attrs.get("start_at", getattr(self.instance, "start_at", None))
        if end_at and start_at and end_at <= start_at:
            raise serializers.ValidationError({"end_at": "Must be after start_at."})
        return attrs


class ShiftStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=EmployeeShift.Status.choices)


class ShiftSwapRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftSwapRequest
        fields = [
            "id",
            "shift",
            "requesting_membership",
            "target_membership",
            "status",
            "approved_by",
            "created_at",
            "updated_at",
        ]
        # `requesting_membership` is always the caller's own (see view);
        # `status`/`approved_by` only change via the approve/reject actions.
        read_only_fields = ["id", "requesting_membership", "status", "approved_by", "created_at", "updated_at"]


class SwapApprovalSerializer(serializers.Serializer):
    target_membership_id = serializers.UUIDField(required=False)


class TimeOffRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeOffRequest
        fields = [
            "id",
            "membership",
            "start_date",
            "end_date",
            "reason",
            "status",
            "approved_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "membership", "status", "approved_by", "created_at", "updated_at"]

    def validate(self, attrs):
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        return attrs


class PayStubSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayStub
        fields = [
            "id",
            "membership",
            "position",
            "pay_period_start",
            "pay_period_end",
            "regular_hours",
            "overtime_hours",
            "gross_pay",
            "net_pay",
            "breakdown",
            "created_at",
            "updated_at",
        ]
        # Entirely read-only — only employees.payroll.generate_pay_stub creates these.
        read_only_fields = fields


class PayStubGenerateSerializer(serializers.Serializer):
    membership_id = serializers.UUIDField()
    position_id = serializers.UUIDField()
    pay_period_start = serializers.DateField()
    pay_period_end = serializers.DateField()

    def validate(self, attrs):
        if attrs["pay_period_end"] < attrs["pay_period_start"]:
            raise serializers.ValidationError({"pay_period_end": "Must be on or after pay_period_start."})
        return attrs
