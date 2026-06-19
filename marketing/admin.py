from django.contrib import admin

from .models import (
    FormSubmission,
    GoogleAdsCampaign,
    Lead,
    PageView,
    TrackingEvent,
    TrackingScript,
    WebsiteVisitor,
)


@admin.register(TrackingScript)
class TrackingScriptAdmin(admin.ModelAdmin):
    list_display = ("business", "script_key", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("business__name", "script_key")
    autocomplete_fields = ["business"]


@admin.register(WebsiteVisitor)
class WebsiteVisitorAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "first_seen", "last_seen", "is_suspicious")
    list_filter = ("is_suspicious",)
    search_fields = ("business__name",)
    autocomplete_fields = ["business"]


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ("visitor", "url", "timestamp")
    search_fields = ("url", "visitor__id")
    autocomplete_fields = ["visitor"]


@admin.register(TrackingEvent)
class TrackingEventAdmin(admin.ModelAdmin):
    list_display = ("visitor", "event_type", "timestamp")
    list_filter = ("event_type",)
    autocomplete_fields = ["visitor"]


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "email", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("name", "email", "business__name")
    autocomplete_fields = ["business", "google_ads_campaign"]


@admin.register(FormSubmission)
class FormSubmissionAdmin(admin.ModelAdmin):
    # ip_address intentionally visible here (admin = the abuse-investigation
    # tool referenced in models.py), even though it's excluded from the API.
    list_display = ("business", "lead", "ip_address", "submitted_at")
    search_fields = ("business__name", "ip_address")
    autocomplete_fields = ["business", "lead"]


@admin.register(GoogleAdsCampaign)
class GoogleAdsCampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "status", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "business__name", "external_campaign_id")
    autocomplete_fields = ["business"]
    # access_token/refresh_token deliberately not in any fieldset — admin
    # uses ModelForm's default field set otherwise, which would render
    # the (decrypted) token in a plain text input. Excluding them keeps
    # this view read/manage-metadata-only; tokens are set via the API.
    exclude = ["access_token", "refresh_token"]
