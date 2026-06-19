from django.contrib import admin

from .models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "status", "size", "uploaded_by", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "business__name", "storage_key")
    autocomplete_fields = ["business", "uploaded_by"]
