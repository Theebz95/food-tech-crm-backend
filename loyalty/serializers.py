"""
Loyalty/order/gift-card serializers.

Order line items reuse finance.serializers.LineItemInputSerializer
directly (same description/quantity/unit_price shape, same validation) —
"stay consistent with what Invoice/Bill chose" applies to the input
serializer too, not just the calculation function. Balance fields
(`available_points`, `lifetime_points`, `current_tier`,
`current_balance`) are read-only everywhere; the only way they change is
through the dedicated award/redeem/reload actions (views.py) calling
loyalty/services.py.
"""

from decimal import Decimal

from rest_framework import serializers

from customers.models import Customer
from finance.serializers import LineItemInputSerializer
from finance.tax import DiscountType, TaxType

from .models import (
    CustomerLoyaltyAccount,
    GiftCard,
    GiftCardTransaction,
    LoyaltyProgram,
    Order,
    OrderLineItem,
    PointsTransaction,
)


def _validate_customer_belongs_to_business(customer, context):
    business = context.get("business")
    if business is not None and customer.business_id != business.id:
        raise serializers.ValidationError("Customer does not belong to this business.")
    return customer


# --- Orders ----------------------------------------------------------------------


class OrderLineItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = OrderLineItem
        fields = ["id", "description", "quantity", "unit_price", "sort_order", "line_total"]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    """Output-only — writes go through OrderWriteSerializer + services.create_order_and_award_points."""

    line_items = OrderLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "business",
            "customer",
            "discount_type",
            "discount_value",
            "tax_type",
            "subtotal",
            "discount_amount",
            "taxable_amount",
            "tax_amount",
            "total",
            "status",
            "notes",
            "line_items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class OrderWriteSerializer(serializers.Serializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    loyalty_program = serializers.PrimaryKeyRelatedField(
        queryset=LoyaltyProgram.objects.all(), required=False, allow_null=True
    )
    discount_type = serializers.ChoiceField(choices=DiscountType.choices, required=False, default=DiscountType.NONE)
    discount_value = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0"))
    tax_type = serializers.ChoiceField(choices=TaxType.choices, required=False, default=TaxType.ZERO)
    notes = serializers.CharField(required=False, allow_blank=True)
    line_items = LineItemInputSerializer(many=True)

    def validate_line_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one line item is required.")
        return value

    def validate_customer(self, customer):
        return _validate_customer_belongs_to_business(customer, self.context)

    def validate_loyalty_program(self, program):
        business = self.context.get("business")
        if program is not None and business is not None and program.business_id != business.id:
            raise serializers.ValidationError("Loyalty program does not belong to this business.")
        return program


# --- Loyalty program / accounts / points ------------------------------------------


class LoyaltyProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltyProgram
        fields = [
            "id",
            "business",
            "name",
            "points_per_dollar",
            "silver_threshold",
            "gold_threshold",
            "platinum_threshold",
            "points_expire_after_days",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate(self, attrs):
        silver = attrs.get("silver_threshold", getattr(self.instance, "silver_threshold", None))
        gold = attrs.get("gold_threshold", getattr(self.instance, "gold_threshold", None))
        platinum = attrs.get("platinum_threshold", getattr(self.instance, "platinum_threshold", None))
        if silver is not None and gold is not None and platinum is not None:
            if not (silver <= gold <= platinum):
                raise serializers.ValidationError("Thresholds must satisfy silver <= gold <= platinum.")
        return attrs


class CustomerLoyaltyAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerLoyaltyAccount
        fields = [
            "id",
            "customer",
            "loyalty_program",
            "available_points",
            "lifetime_points",
            "current_tier",
            "created_at",
            "updated_at",
        ]
        # Balances/tier are only ever changed by award_points/redeem_points
        # — never a direct field write, even on create.
        read_only_fields = ["id", "available_points", "lifetime_points", "current_tier", "created_at", "updated_at"]

    def validate_customer(self, customer):
        return _validate_customer_belongs_to_business(customer, self.context)

    def validate_loyalty_program(self, program):
        business = self.context.get("business")
        if business is not None and program.business_id != business.id:
            raise serializers.ValidationError("Loyalty program does not belong to this business.")
        return program

    def validate(self, attrs):
        customer = attrs.get("customer")
        program = attrs.get("loyalty_program")
        if customer is not None and program is not None:
            if CustomerLoyaltyAccount.objects.filter(customer=customer, loyalty_program=program).exists():
                raise serializers.ValidationError("This customer already has an account on this program.")
        return attrs


class PointsTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PointsTransaction
        fields = ["id", "account", "points_change", "reason", "order", "notes", "expires_at", "created_at"]
        read_only_fields = fields


class AwardPointsSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True)


class RedeemPointsSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True)


# --- Gift cards --------------------------------------------------------------------


class GiftCardSerializer(serializers.ModelSerializer):
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = GiftCard
        fields = [
            "id",
            "business",
            "code",
            "initial_balance",
            "current_balance",
            "is_active",
            "is_expired",
            "expires_at",
            "recipient_name",
            "recipient_email",
            "purchaser_customer",
            "sent_at",
            "created_at",
            "updated_at",
        ]
        # code/balances/sent_at are server-managed — code at creation,
        # balances only via reload/redeem, sent_at only via the send action.
        read_only_fields = [
            "id",
            "business",
            "code",
            "initial_balance",
            "current_balance",
            "is_expired",
            "sent_at",
            "created_at",
            "updated_at",
        ]

    def validate_purchaser_customer(self, customer):
        if customer is not None:
            return _validate_customer_belongs_to_business(customer, self.context)
        return customer


class GiftCardCreateSerializer(serializers.Serializer):
    """Input-only — create() goes through services.create_gift_card, not a generic ModelSerializer.save()."""

    initial_balance = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    recipient_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    recipient_email = serializers.EmailField(required=False, allow_blank=True, max_length=254)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    purchaser_customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(), required=False, allow_null=True
    )

    def validate_purchaser_customer(self, customer):
        if customer is not None:
            return _validate_customer_belongs_to_business(customer, self.context)
        return customer


class ReloadGiftCardSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    notes = serializers.CharField(required=False, allow_blank=True)


class RedeemGiftCardSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    notes = serializers.CharField(required=False, allow_blank=True)


class GiftCardTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GiftCardTransaction
        fields = ["id", "gift_card", "amount_change", "reason", "notes", "created_by", "created_at"]
        read_only_fields = fields
