"""
Marketing serializers.

The public input serializers (TrackEventSerializer, FormSubmitSerializer)
deliberately have no `business`/`visitor` field at all — those are
resolved server-side from `script_key` and the visitor cookie
respectively (see public_views.py), never accepted from the request
body. Field-shape validation errors on these (missing field, wrong type)
return normal DRF field errors; only `script_key` *resolution* (see
public_views.py) gets the generic, indistinguishable rejection response —
payload shape isn't secret, but whether a key exists/is active is exactly
the thing that must not be probeable.
"""

import json

from django.core.validators import RegexValidator
from rest_framework import serializers

from .models import FormSubmission, GoogleAdsCampaign, Lead, PageView, TrackingEvent, TrackingScript, WebsiteVisitor

phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)

# Size caps for the public, unauthenticated JSONFields — an unvalidated
# JSONField on a public endpoint is an open invitation to store
# arbitrarily large payloads. Chosen generously for real event/form data
# (a handful of custom properties, or a contact form's fields) while
# clearly bounding the worst case.
MAX_METADATA_BYTES = 4096
MAX_FORM_DATA_BYTES = 8192


def _validate_json_object_size(value, max_bytes, field_name):
    if not isinstance(value, dict):
        raise serializers.ValidationError(f"{field_name} must be a JSON object.")
    size = len(json.dumps(value).encode("utf-8"))
    if size > max_bytes:
        raise serializers.ValidationError(f"{field_name} payload too large ({size} bytes, max {max_bytes}).")
    return value


# --- Public input -------------------------------------------------------------


class TrackEventSerializer(serializers.Serializer):
    script_key = serializers.CharField(max_length=64)
    kind = serializers.ChoiceField(choices=["pageview", "event"])
    url = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    referrer = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    event_type = serializers.ChoiceField(choices=TrackingEvent.EventType.choices, required=False)
    metadata = serializers.JSONField(required=False)

    def validate_metadata(self, value):
        return _validate_json_object_size(value, MAX_METADATA_BYTES, "metadata")

    def validate(self, attrs):
        if attrs["kind"] == "pageview" and not attrs.get("url"):
            raise serializers.ValidationError({"url": "Required when kind is 'pageview'."})
        if attrs["kind"] == "event" and not attrs.get("event_type"):
            raise serializers.ValidationError({"event_type": "Required when kind is 'event'."})
        attrs.setdefault("metadata", {})
        attrs.setdefault("referrer", "")
        return attrs


class FormSubmitSerializer(serializers.Serializer):
    script_key = serializers.CharField(max_length=64)
    form_data = serializers.JSONField()

    def validate_form_data(self, value):
        return _validate_json_object_size(value, MAX_FORM_DATA_BYTES, "form_data")


# --- Staff-side ----------------------------------------------------------------


class TrackingScriptSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingScript
        fields = ["id", "business", "script_key", "is_active", "created_at", "updated_at"]
        # script_key is always server-generated (create, and the
        # regenerate-key action) — never client-chosen.
        read_only_fields = ["id", "business", "script_key", "created_at", "updated_at"]


class WebsiteVisitorSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebsiteVisitor
        fields = ["id", "business", "first_seen", "last_seen", "is_suspicious", "flagged_at"]
        read_only_fields = fields


class PageViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageView
        fields = ["id", "visitor", "url", "referrer", "timestamp"]
        read_only_fields = fields


class TrackingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingEvent
        fields = ["id", "visitor", "event_type", "metadata", "timestamp"]
        read_only_fields = fields


class GoogleAdsCampaignSerializer(serializers.ModelSerializer):
    # Write-only: accepted on create/update, never returned — encrypted
    # or not, an OAuth token has no business appearing in an API response.
    access_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    refresh_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = GoogleAdsCampaign
        fields = [
            "id",
            "business",
            "name",
            "external_campaign_id",
            "status",
            "is_active",
            "access_token",
            "refresh_token",
            "token_expires_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]


class LeadSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(required=False, allow_blank=True, validators=[phone_validator], max_length=32)
    email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Lead
        fields = [
            "id",
            "business",
            "name",
            "email",
            "phone",
            "source",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "google_ads_campaign",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate_google_ads_campaign(self, campaign):
        business = self.context.get("business")
        if campaign is not None and business is not None and campaign.business_id != business.id:
            raise serializers.ValidationError("Campaign does not belong to this business.")
        return campaign

    def validate(self, attrs):
        business = self.context.get("business") or getattr(self.instance, "business", None)
        email = attrs.get("email", getattr(self.instance, "email", ""))
        if business and email:
            qs = Lead.objects.filter(business=business, email=email)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"email": "A lead with this email already exists for this business."})
        return attrs


class FormSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FormSubmission
        # ip_address deliberately excluded — see models.py FormSubmission docstring.
        fields = ["id", "business", "lead", "form_data", "submitted_at"]
        read_only_fields = fields
