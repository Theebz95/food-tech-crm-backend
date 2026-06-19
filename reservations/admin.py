from django.contrib import admin

from .models import (
    BlackoutDate,
    BusinessHours,
    FloorPlan,
    Reservation,
    ReservationSetting,
    RestaurantTable,
    Waitlist,
)


@admin.register(RestaurantTable)
class RestaurantTableAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "capacity", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "location__name")
    autocomplete_fields = ["location"]


@admin.register(FloorPlan)
class FloorPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "location__name")
    autocomplete_fields = ["location"]


@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("location", "day_of_week", "open_time", "close_time", "is_closed")
    list_filter = ("day_of_week", "is_closed")
    autocomplete_fields = ["location"]


@admin.register(BlackoutDate)
class BlackoutDateAdmin(admin.ModelAdmin):
    list_display = ("location", "date", "reason")
    search_fields = ("location__name", "reason")
    autocomplete_fields = ["location"]


@admin.register(ReservationSetting)
class ReservationSettingAdmin(admin.ModelAdmin):
    list_display = ("business", "default_duration_minutes", "max_party_size")
    search_fields = ("business__name",)
    autocomplete_fields = ["business"]


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("guest_name", "location", "table", "start_time", "status", "confirmation_code")
    list_filter = ("status",)
    search_fields = ("guest_name", "guest_email", "guest_phone", "confirmation_code")
    autocomplete_fields = ["location", "table"]


@admin.register(Waitlist)
class WaitlistAdmin(admin.ModelAdmin):
    list_display = ("guest_name", "location", "party_size", "requested_time", "status")
    list_filter = ("status",)
    search_fields = ("guest_name", "guest_email", "guest_phone")
    autocomplete_fields = ["location"]
