"""
Finance serializers.

Invoice/Estimate creation and editing go through dedicated "write"
serializers (plain Serializer, not ModelSerializer) whose validated_data
is handed to finance/services.py — never a generic ModelSerializer.save(),
since totals/invoice_number/status are server-computed, not client fields.
The read serializers (InvoiceSerializer/EstimateSerializer) are what every
other action returns.
"""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from customers.models import Customer
from inventory.models import Vendor

from . import services
from .models import (
    BankTransaction,
    Bill,
    BillLineItem,
    BillPayment,
    ChartOfAccount,
    Estimate,
    EstimateLineItem,
    Invoice,
    InvoiceLineItem,
    InvoiceTemplate,
    Payment,
    RecurringTransaction,
)
from .tax import DiscountType, TaxType


class ChartOfAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChartOfAccount
        fields = ["id", "business", "code", "name", "account_type", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "business", "created_at", "updated_at"]


class LineItemInputSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=500)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)


def _validate_account_belongs_to_business(account, context):
    business = context.get("business")
    if account is not None and business is not None and account.business_id != business.id:
        raise serializers.ValidationError("Account does not belong to this business.")
    return account


def _validate_customer_belongs_to_business(customer, context):
    business = context.get("business")
    if business is not None and customer.business_id != business.id:
        raise serializers.ValidationError("Customer does not belong to this business.")
    return customer


def _validate_vendor_belongs_to_business(vendor, context):
    business = context.get("business")
    if vendor is not None and business is not None and vendor.business_id != business.id:
        raise serializers.ValidationError("Vendor does not belong to this business.")
    return vendor


def _validate_line_item_presets(value):
    """Shared by InvoiceTemplateSerializer and RecurringTransactionSerializer — same preset shape, same validation."""
    if not isinstance(value, list):
        raise serializers.ValidationError("Must be a list.")
    for entry in value:
        if not isinstance(entry, dict) or not {"description", "quantity", "unit_price"} <= entry.keys():
            raise serializers.ValidationError('Each entry must have "description", "quantity", and "unit_price".')
    return value


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceLineItem
        fields = ["id", "description", "quantity", "unit_price", "sort_order", "line_total"]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    """Output-only — see module docstring; writes go through InvoiceWriteSerializer + services.py."""

    line_items = InvoiceLineItemSerializer(many=True, read_only=True)
    paid_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id",
            "business",
            "customer",
            "revenue_account",
            "invoice_number",
            "discount_type",
            "discount_value",
            "tax_type",
            "subtotal",
            "discount_amount",
            "taxable_amount",
            "tax_amount",
            "total",
            "status",
            "due_date",
            "sent_at",
            "notes",
            "line_items",
            "paid_total",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class InvoiceWriteSerializer(serializers.Serializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    revenue_account = serializers.PrimaryKeyRelatedField(
        queryset=ChartOfAccount.objects.all(), required=False, allow_null=True
    )
    discount_type = serializers.ChoiceField(choices=DiscountType.choices, required=False, default=DiscountType.NONE)
    discount_value = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0"))
    tax_type = serializers.ChoiceField(choices=TaxType.choices, required=False, default=TaxType.ZERO)
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    line_items = LineItemInputSerializer(many=True, required=False)

    def validate_line_items(self, value):
        if self.partial and value is None:
            return value
        if not value:
            raise serializers.ValidationError("At least one line item is required.")
        return value

    def validate_customer(self, customer):
        return _validate_customer_belongs_to_business(customer, self.context)

    def validate_revenue_account(self, account):
        return _validate_account_belongs_to_business(account, self.context)


class RecordPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=Payment.Method.choices)
    stripe_payment_intent_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    deposit_account = serializers.PrimaryKeyRelatedField(
        queryset=ChartOfAccount.objects.all(), required=False, allow_null=True
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_deposit_account(self, account):
        return _validate_account_belongs_to_business(account, self.context)


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "business",
            "invoice",
            "deposit_account",
            "amount",
            "method",
            "stripe_payment_intent_id",
            "notes",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class EstimateLineItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = EstimateLineItem
        fields = ["id", "description", "quantity", "unit_price", "sort_order", "line_total"]
        read_only_fields = fields


class EstimateSerializer(serializers.ModelSerializer):
    line_items = EstimateLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Estimate
        fields = [
            "id",
            "business",
            "customer",
            "estimate_number",
            "discount_type",
            "discount_value",
            "tax_type",
            "subtotal",
            "discount_amount",
            "taxable_amount",
            "tax_amount",
            "total",
            "status",
            "expires_at",
            "notes",
            "converted_invoice",
            "line_items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class EstimateWriteSerializer(serializers.Serializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    discount_type = serializers.ChoiceField(choices=DiscountType.choices, required=False, default=DiscountType.NONE)
    discount_value = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0"))
    tax_type = serializers.ChoiceField(choices=TaxType.choices, required=False, default=TaxType.ZERO)
    expires_at = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # "converted" is only ever set by the convert-to-invoice action.
    status = serializers.ChoiceField(
        choices=[c for c in Estimate.Status.choices if c[0] != Estimate.Status.CONVERTED], required=False
    )
    line_items = LineItemInputSerializer(many=True, required=False)

    def validate_line_items(self, value):
        if self.partial and value is None:
            return value
        if not value:
            raise serializers.ValidationError("At least one line item is required.")
        return value

    def validate_customer(self, customer):
        return _validate_customer_belongs_to_business(customer, self.context)


class InvoiceTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceTemplate
        fields = [
            "id",
            "business",
            "name",
            "line_item_presets",
            "default_tax_type",
            "default_notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate_line_item_presets(self, value):
        return _validate_line_item_presets(value)


# --- Bill / BillPayment ---------------------------------------------------------


class BillLineItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = BillLineItem
        fields = ["id", "description", "quantity", "unit_price", "sort_order", "line_total"]
        read_only_fields = fields


class BillSerializer(serializers.ModelSerializer):
    """Output-only — mirrors InvoiceSerializer; writes go through BillWriteSerializer + services.py."""

    line_items = BillLineItemSerializer(many=True, read_only=True)
    paid_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Bill
        fields = [
            "id",
            "business",
            "vendor",
            "expense_account",
            "bill_number",
            "discount_type",
            "discount_value",
            "tax_type",
            "subtotal",
            "discount_amount",
            "taxable_amount",
            "tax_amount",
            "total",
            "status",
            "due_date",
            "received_at",
            "notes",
            "line_items",
            "paid_total",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class BillWriteSerializer(serializers.Serializer):
    vendor = serializers.PrimaryKeyRelatedField(queryset=Vendor.objects.all())
    expense_account = serializers.PrimaryKeyRelatedField(
        queryset=ChartOfAccount.objects.all(), required=False, allow_null=True
    )
    discount_type = serializers.ChoiceField(choices=DiscountType.choices, required=False, default=DiscountType.NONE)
    discount_value = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0"))
    tax_type = serializers.ChoiceField(choices=TaxType.choices, required=False, default=TaxType.ZERO)
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    line_items = LineItemInputSerializer(many=True, required=False)

    def validate_line_items(self, value):
        if self.partial and value is None:
            return value
        if not value:
            raise serializers.ValidationError("At least one line item is required.")
        return value

    def validate_vendor(self, vendor):
        return _validate_vendor_belongs_to_business(vendor, self.context)

    def validate_expense_account(self, account):
        return _validate_account_belongs_to_business(account, self.context)


class RecordBillPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=BillPayment.Method.choices)
    payment_account = serializers.PrimaryKeyRelatedField(
        queryset=ChartOfAccount.objects.all(), required=False, allow_null=True
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_payment_account(self, account):
        return _validate_account_belongs_to_business(account, self.context)


class BillPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillPayment
        fields = [
            "id",
            "business",
            "bill",
            "payment_account",
            "amount",
            "method",
            "notes",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


# --- BankTransaction -------------------------------------------------------------


# Allow-list mirroring services.RECONCILIATION_MODELS' keys — kept in
# sync deliberately; see BankTransaction.reconciled_object's docstring
# for why a bare GenericForeignKey needs this restriction layered on top.
RECONCILIATION_TARGET_CHOICES = list(services.RECONCILIATION_MODELS.keys())


class BankTransactionSerializer(serializers.ModelSerializer):
    reconciled_target_type = serializers.SerializerMethodField()

    class Meta:
        model = BankTransaction
        fields = [
            "id",
            "business",
            "date",
            "description",
            "amount",
            "source",
            "external_transaction_id",
            "is_reconciled",
            "reconciled_object_id",
            "reconciled_target_type",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "business",
            "is_reconciled",
            "reconciled_object_id",
            "reconciled_target_type",
            "created_at",
            "updated_at",
        ]

    def get_reconciled_target_type(self, obj):
        return obj.reconciled_content_type.model if obj.reconciled_content_type_id else None


class ReconcileBankTransactionSerializer(serializers.Serializer):
    target_type = serializers.ChoiceField(choices=RECONCILIATION_TARGET_CHOICES)
    object_id = serializers.UUIDField()

    def validate(self, attrs):
        model = services.RECONCILIATION_MODELS[attrs["target_type"]]
        target = model.objects.filter(pk=attrs["object_id"]).first()
        if target is None:
            raise serializers.ValidationError({"object_id": "No matching object found."})
        attrs["target_object"] = target
        return attrs


# --- RecurringTransaction --------------------------------------------------------


class RecurringTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecurringTransaction
        fields = [
            "id",
            "business",
            "kind",
            "customer",
            "vendor",
            "line_item_presets",
            "discount_type",
            "discount_value",
            "tax_type",
            "notes",
            "due_in_days",
            "recurrence_rule",
            "start_date",
            "end_date",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "business", "created_at", "updated_at"]

    def validate_line_item_presets(self, value):
        return _validate_line_item_presets(value)

    def validate_customer(self, customer):
        return _validate_customer_belongs_to_business(customer, self.context)

    def validate_vendor(self, vendor):
        return _validate_vendor_belongs_to_business(vendor, self.context)

    def validate(self, attrs):
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        customer = attrs.get("customer", getattr(self.instance, "customer", None))
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))

        if kind == RecurringTransaction.Kind.INVOICE:
            if customer is None:
                raise serializers.ValidationError({"customer": "Required when kind is 'invoice'."})
            if vendor is not None:
                raise serializers.ValidationError({"vendor": "Must not be set when kind is 'invoice'."})
        elif kind == RecurringTransaction.Kind.BILL:
            if vendor is None:
                raise serializers.ValidationError({"vendor": "Required when kind is 'bill'."})
            if customer is not None:
                raise serializers.ValidationError({"customer": "Must not be set when kind is 'bill'."})

        line_item_presets = attrs.get("line_item_presets", getattr(self.instance, "line_item_presets", None))
        if not line_item_presets:
            raise serializers.ValidationError({"line_item_presets": "At least one line item preset is required."})

        return attrs
