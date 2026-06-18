"""
Customer serializers.

Email/phone validation lives here, not just on the model fields, per the
Phase 1 audit finding that the old system only validated these client-side
(trivially bypassed by hitting the API directly). DRF runs these validators
on every write regardless of what the client sent.
"""

from django.core.validators import RegexValidator
from rest_framework import serializers

from .models import Customer

# E.164-ish: optional leading +, 7-15 digits total, no letters/punctuation.
# Deliberately permissive on formatting (callers may send "+1 555-..." etc
# in the future) is NOT done here — validation is strict by design so bad
# data can't silently get stored; reformat client-side before submitting.
phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)


class CustomerSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(
        required=False, allow_blank=True, validators=[phone_validator], max_length=32
    )
    # EmailField already validates format; explicit here so it's obvious
    # this is enforced server-side and so blank stays allowed (phone-only
    # customers are valid).
    email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = Customer
        fields = [
            "id",
            "business",
            "name",
            "email",
            "phone",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        # `business` is set by the view from the URL (the business the
        # caller has a membership for), never accepted from the request
        # body — otherwise a client could smuggle a different business_id
        # in the payload and write into a tenant it has no membership for.
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate(self, attrs):
        # `business` is read-only (set by the view from the URL, see
        # CustomerViewSet) so it's never in `attrs` on create — pull it
        # from context instead, falling back to the existing instance on
        # update.
        business = self.context.get("business") or getattr(self.instance, "business", None)
        email = attrs.get("email", getattr(self.instance, "email", ""))
        if business and email:
            qs = Customer.objects.filter(business=business, email=email)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"email": "A customer with this email already exists for this business."}
                )
        return attrs
