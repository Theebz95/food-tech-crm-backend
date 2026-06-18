from django.contrib import admin

from .models import Business, BusinessLocation, BusinessMembership


class BusinessLocationInline(admin.TabularInline):
    model = BusinessLocation
    extra = 0


class BusinessMembershipInline(admin.TabularInline):
    model = BusinessMembership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "is_active", "subscription_status", "trial_ends_at", "is_legacy")
    list_filter = ("is_active", "is_legacy", "subscription_status")
    search_fields = ("name", "owner__email")
    inlines = [BusinessLocationInline, BusinessMembershipInline]


@admin.register(BusinessLocation)
class BusinessLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "is_active")
    search_fields = ("name", "business__name")


@admin.register(BusinessMembership)
class BusinessMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "business", "role", "location", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("user__email", "business__name")
    autocomplete_fields = ["user", "business", "location"]
