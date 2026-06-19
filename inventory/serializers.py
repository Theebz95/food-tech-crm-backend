from rest_framework import serializers

from .models import InventoryItem, InventoryTransaction, Vendor


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = [
            "id",
            "business",
            "name",
            "contact_name",
            "contact_email",
            "contact_phone",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        # `business` is set by the view from the URL, same pattern as CustomerSerializer.
        read_only_fields = ["id", "business", "created_at", "updated_at"]


class InventoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryItem
        fields = [
            "id",
            "business",
            "location",
            "vendor",
            "name",
            "unit",
            "current_quantity",
            "low_stock_threshold",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate_location(self, location):
        business = self.context.get("business")
        if location is not None and business is not None and location.business_id != business.id:
            raise serializers.ValidationError("Location does not belong to this business.")
        return location

    def validate_vendor(self, vendor):
        business = self.context.get("business")
        if vendor is not None and business is not None and vendor.business_id != business.id:
            raise serializers.ValidationError("Vendor does not belong to this business.")
        return vendor

    def validate(self, attrs):
        # current_quantity is writable on create (a starting balance, not
        # a logged change) but locked on update — every change after
        # creation must go through the adjust-stock action so it's always
        # recorded in InventoryTransaction. See inventory/services.py.
        if self.instance is not None and "current_quantity" in attrs:
            raise serializers.ValidationError(
                {"current_quantity": "Cannot be changed directly — use the adjust-stock action."}
            )
        return attrs


class InventoryTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryTransaction
        fields = ["id", "item", "quantity_change", "transaction_type", "reason", "created_by", "created_at"]
        # Entirely read-only — the only way one of these gets created is
        # services.adjust_stock; there is no generic create/update/delete
        # for the ledger (see views.py and InventoryTransaction.save()).
        read_only_fields = fields


class AdjustStockSerializer(serializers.Serializer):
    delta = serializers.DecimalField(max_digits=12, decimal_places=3)
    transaction_type = serializers.ChoiceField(choices=InventoryTransaction.TransactionType.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Must be nonzero.")
        return value
