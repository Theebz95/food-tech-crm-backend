from django.contrib import admin

from .models import InventoryItem, InventoryTransaction, Vendor


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "contact_email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name", "contact_email")
    autocomplete_fields = ["business"]


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "current_quantity", "unit", "low_stock_threshold", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name")
    autocomplete_fields = ["business", "location", "vendor"]


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ("item", "quantity_change", "transaction_type", "created_by", "created_at")
    list_filter = ("transaction_type",)
    search_fields = ("item__name",)
    autocomplete_fields = ["item", "created_by"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
