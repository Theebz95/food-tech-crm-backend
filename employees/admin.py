from django.contrib import admin

from .models import (
    EmployeeAvailability,
    EmployeeShift,
    GeofenceSetting,
    LocationVerificationLog,
    PayStub,
    Position,
    RecurringSchedule,
    ShiftSwapRequest,
    ShiftTemplate,
    TimeEntry,
    TimeEntryBreak,
    TimeOffRequest,
)


@admin.register(GeofenceSetting)
class GeofenceSettingAdmin(admin.ModelAdmin):
    list_display = ("business", "location", "radius_meters", "enabled")
    list_filter = ("enabled",)
    search_fields = ("business__name", "location__name")
    autocomplete_fields = ["business", "location"]


class TimeEntryBreakInline(admin.TabularInline):
    model = TimeEntryBreak
    extra = 0


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("membership", "clock_in_at", "clock_out_at", "status", "clock_in_within_geofence")
    list_filter = ("status", "clock_in_within_geofence", "clock_out_within_geofence")
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership"]
    inlines = [TimeEntryBreakInline]


@admin.register(LocationVerificationLog)
class LocationVerificationLogAdmin(admin.ModelAdmin):
    list_display = ("membership", "check_type", "passed", "distance_meters", "created_at")
    list_filter = ("check_type", "passed")
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership", "time_entry", "geofence_setting"]


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "hourly_rate", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name")
    autocomplete_fields = ["business"]


@admin.register(EmployeeAvailability)
class EmployeeAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("membership", "day_of_week", "start_time", "end_time")
    list_filter = ("day_of_week",)
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership"]


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "location", "position", "day_of_week", "start_time", "end_time", "is_active")
    list_filter = ("day_of_week", "is_active")
    search_fields = ("name", "business__name", "position__name")
    autocomplete_fields = ["business", "location", "position"]


@admin.register(RecurringSchedule)
class RecurringScheduleAdmin(admin.ModelAdmin):
    list_display = ("membership", "shift_template", "recurrence_rule", "start_date", "end_date", "is_active")
    list_filter = ("recurrence_rule", "is_active")
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership", "shift_template"]


@admin.register(EmployeeShift)
class EmployeeShiftAdmin(admin.ModelAdmin):
    list_display = ("membership", "position", "start_at", "end_at", "status", "recurring_schedule")
    list_filter = ("status",)
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership", "position", "recurring_schedule"]


@admin.register(ShiftSwapRequest)
class ShiftSwapRequestAdmin(admin.ModelAdmin):
    list_display = ("shift", "requesting_membership", "target_membership", "status", "approved_by")
    list_filter = ("status",)
    search_fields = ("requesting_membership__user__email",)
    autocomplete_fields = ["shift", "requesting_membership", "target_membership", "approved_by"]


@admin.register(TimeOffRequest)
class TimeOffRequestAdmin(admin.ModelAdmin):
    list_display = ("membership", "start_date", "end_date", "status", "approved_by")
    list_filter = ("status",)
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership", "approved_by"]


@admin.register(PayStub)
class PayStubAdmin(admin.ModelAdmin):
    list_display = ("membership", "position", "pay_period_start", "pay_period_end", "gross_pay", "net_pay")
    search_fields = ("membership__user__email", "membership__business__name")
    autocomplete_fields = ["membership", "position"]
