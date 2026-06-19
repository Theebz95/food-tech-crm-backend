from decimal import Decimal

from rest_framework import serializers

from .models import GeofenceSetting, TimeEntry, TimeEntryBreak


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
