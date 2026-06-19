from rest_framework import serializers

from .models import Document


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            "id",
            "business",
            "name",
            "storage_key",
            "content_type",
            "size",
            "status",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]
        # Entirely read-only — every field is set by services.upload_document;
        # there is no generic create/update, only the upload action (views.py).
        read_only_fields = fields


class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    name = serializers.CharField(required=False, max_length=255)

    def validate(self, attrs):
        if not attrs.get("name"):
            attrs["name"] = attrs["file"].name
        return attrs
