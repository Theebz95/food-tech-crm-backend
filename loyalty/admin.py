from django.contrib import admin

from .models import (
    CustomerLoyaltyAccount,
    GiftCard,
    GiftCardTransaction,
    LoyaltyProgram,
    Order,
    OrderLineItem,
    PointsTransaction,
)


class OrderLineItemInline(admin.TabularInline):
    model = OrderLineItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "customer", "status", "total", "created_at")
    list_filter = ("status",)
    search_fields = ("business__name", "customer__name")
    autocomplete_fields = ["business", "customer"]
    inlines = [OrderLineItemInline]


@admin.register(LoyaltyProgram)
class LoyaltyProgramAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "points_per_dollar", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name")
    autocomplete_fields = ["business"]


@admin.register(CustomerLoyaltyAccount)
class CustomerLoyaltyAccountAdmin(admin.ModelAdmin):
    list_display = ("customer", "loyalty_program", "available_points", "lifetime_points", "current_tier")
    list_filter = ("current_tier",)
    search_fields = ("customer__name", "loyalty_program__name")
    autocomplete_fields = ["customer", "loyalty_program"]


@admin.register(PointsTransaction)
class PointsTransactionAdmin(admin.ModelAdmin):
    list_display = ("account", "points_change", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("account__customer__name",)
    autocomplete_fields = ["account", "order", "expired_transaction"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GiftCard)
class GiftCardAdmin(admin.ModelAdmin):
    list_display = ("code", "business", "current_balance", "is_active", "expires_at")
    list_filter = ("is_active",)
    search_fields = ("code", "business__name", "recipient_email")
    autocomplete_fields = ["business", "purchaser_customer"]


@admin.register(GiftCardTransaction)
class GiftCardTransactionAdmin(admin.ModelAdmin):
    list_display = ("gift_card", "amount_change", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("gift_card__code",)
    autocomplete_fields = ["gift_card", "created_by"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
