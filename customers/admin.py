from django.contrib import admin

from .models import Customer, CustomerBusinessLink, CustomerProfile


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "email", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "email", "phone", "business__name")
    autocomplete_fields = ["business"]


class CustomerBusinessLinkInline(admin.TabularInline):
    model = CustomerBusinessLink
    extra = 0
    autocomplete_fields = ["business"]


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "user", "email_verified")
    list_filter = ("email_verified",)
    search_fields = ("full_name", "user__email")
    autocomplete_fields = ["user"]
    inlines = [CustomerBusinessLinkInline]


@admin.register(CustomerBusinessLink)
class CustomerBusinessLinkAdmin(admin.ModelAdmin):
    list_display = ("customer_profile", "business", "created_at")
    search_fields = ("customer_profile__full_name", "business__name")
    autocomplete_fields = ["customer_profile", "business"]
