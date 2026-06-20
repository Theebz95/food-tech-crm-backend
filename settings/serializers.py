from django.core.validators import RegexValidator
from rest_framework import serializers

from documents.serializers import DocumentSerializer

from .models import BusinessProfile

phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)


class BusinessProfileSerializer(serializers.ModelSerializer):
    # Reuses documents.DocumentSerializer wholesale rather than re-deriving
    # a presigned-URL field here — the actual file URL comes from the
    # existing GET .../documents/<id>/download/ endpoint, using this id.
    logo = DocumentSerializer(read_only=True)
    contact_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    contact_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = BusinessProfile
        fields = [
            "id",
            "business",
            "logo",
            "contact_email",
            "contact_phone",
            "address",
            "default_timezone",
            "email_on_new_reservation",
            "email_on_low_stock",
            "email_on_new_lead",
            "created_at",
            "updated_at",
        ]
        # `logo` is only ever set via the upload-logo/remove-logo actions
        # (views.py -> settings.services), never a direct field write.
        read_only_fields = ["id", "business", "logo", "created_at", "updated_at"]


class LogoUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    name = serializers.CharField(required=False, max_length=255)

    def validate(self, attrs):
        if not attrs.get("name"):
            attrs["name"] = attrs["file"].name
        return attrs
