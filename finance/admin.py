from django.contrib import admin

from .models import (
    BankTransaction,
    Bill,
    BillLineItem,
    BillPayment,
    ChartOfAccount,
    Estimate,
    EstimateLineItem,
    Invoice,
    InvoiceLineItem,
    InvoiceTemplate,
    Payment,
    RecurringTransaction,
    Refund,
    StripeWebhookEvent,
)


@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "business", "account_type", "is_active")
    list_filter = ("account_type", "is_active")
    search_fields = ("name", "code", "business__name")
    autocomplete_fields = ["business"]


class InvoiceLineItemInline(admin.TabularInline):
    model = InvoiceLineItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "business", "customer", "status", "total", "due_date")
    list_filter = ("status",)
    search_fields = ("invoice_number", "business__name", "customer__name")
    autocomplete_fields = ["business", "customer", "revenue_account"]
    inlines = [InvoiceLineItemInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("amount", "method", "business", "invoice", "created_at")
    list_filter = ("method",)
    search_fields = ("business__name", "invoice__invoice_number", "stripe_payment_intent_id")
    autocomplete_fields = ["business", "invoice", "deposit_account", "created_by"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("amount", "payment", "business", "created_at")
    search_fields = ("business__name", "payment__invoice__invoice_number")
    autocomplete_fields = ["business", "payment", "created_by"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EstimateLineItemInline(admin.TabularInline):
    model = EstimateLineItem
    extra = 0


@admin.register(Estimate)
class EstimateAdmin(admin.ModelAdmin):
    list_display = ("estimate_number", "business", "customer", "status", "total", "expires_at")
    list_filter = ("status",)
    search_fields = ("estimate_number", "business__name", "customer__name")
    autocomplete_fields = ["business", "customer", "converted_invoice"]
    inlines = [EstimateLineItemInline]


@admin.register(InvoiceTemplate)
class InvoiceTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name")
    autocomplete_fields = ["business"]


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "received_at")
    list_filter = ("event_type",)
    search_fields = ("event_id",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BillLineItemInline(admin.TabularInline):
    model = BillLineItem
    extra = 0


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ("bill_number", "business", "vendor", "status", "total", "due_date")
    list_filter = ("status",)
    search_fields = ("bill_number", "business__name", "vendor__name")
    autocomplete_fields = ["business", "vendor", "expense_account", "recurring_transaction"]
    inlines = [BillLineItemInline]


@admin.register(BillPayment)
class BillPaymentAdmin(admin.ModelAdmin):
    list_display = ("amount", "method", "business", "bill", "created_at")
    list_filter = ("method",)
    search_fields = ("business__name", "bill__bill_number")
    autocomplete_fields = ["business", "bill", "payment_account", "created_by"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "description", "amount", "business", "source", "is_reconciled")
    list_filter = ("source", "is_reconciled")
    search_fields = ("description", "business__name", "external_transaction_id")
    autocomplete_fields = ["business"]


@admin.register(RecurringTransaction)
class RecurringTransactionAdmin(admin.ModelAdmin):
    list_display = ("business", "kind", "recurrence_rule", "start_date", "end_date", "is_active")
    list_filter = ("kind", "recurrence_rule", "is_active")
    search_fields = ("business__name",)
    autocomplete_fields = ["business", "customer", "vendor"]
