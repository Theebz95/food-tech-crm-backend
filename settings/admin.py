from django.contrib import admin

from .models import BusinessProfile


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    list_display = ("business", "contact_email", "default_timezone")
    search_fields = ("business__name", "contact_email")
    autocomplete_fields = ["business", "logo"]
