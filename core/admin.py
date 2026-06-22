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

    def has_delete_permission(self, request, obj=None):
        # Business is intentionally non-deletable everywhere — see
        # README "Business is permanent" and core/models.py. Every domain
        # table CASCADEs from Business, so a hard delete would silently
        # destroy financial/payroll/audit history this codebase was
        # otherwise built specifically to protect (append-only ledgers,
        # PROTECT'd Customer/Vendor FKs, etc. — all bypassed at once by
        # deleting the tenant itself). Deactivate (is_active=False)
        # instead; there is no supported path to actually delete one.
        return False


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
