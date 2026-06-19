from django.contrib import admin

from .models import GeofenceSetting, LocationVerificationLog, TimeEntry, TimeEntryBreak


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
