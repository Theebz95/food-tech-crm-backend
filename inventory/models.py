"""
Inventory & Vendors domain.

  vendors             (old) -> Vendor
  inventory_items     (old) -> InventoryItem
  inventory_transactions / inventory_usage (old) -> InventoryTransaction

`InventoryTransaction` is the one ledger for every stock change — restock,
usage, waste, and manual correction are all the same shape (which item,
how much it changed by, who did it, when), distinguished by
`transaction_type`, rather than a separate `InventoryUsage` model with
identical columns and constraints for no behavioral difference. The old
system's `useAdjustStock()`/`useRecordUsage()` hooks were really the same
operation under two names; this keeps that unification rather than
re-introducing the split.

The actual fix (Phase 1 audit finding): stock-level updates used to be a
non-atomic two-step client write (update the item's quantity, then insert
a ledger row) — same risk class as the Loyalty/gift-card balance pattern
documented in `loyalty/models.py`. `inventory/services.py:adjust_stock`
wraps both writes in `transaction.atomic()` + `select_for_update()` on the
`InventoryItem` row, and rejects (raises) rather than silently clamps an
adjustment that would take stock negative.
"""

import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from core.models import Business, BusinessLocation, BusinessMembership

phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Phone number must contain 7-15 digits, optionally prefixed with '+'.",
)


class Vendor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="vendors")
    name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(
        max_length=32, blank=True, default="", validators=[phone_validator]
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_vendor_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return self.name


class InventoryItem(models.Model):
    """
    `location` is optional (null = business-wide) — same convention as
    `GeofenceSetting`/`ShiftTemplate` elsewhere in this codebase, unlike
    the Reservations domain where location is required. Stock for a
    single-location business simply never sets it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="inventory_items")
    location = models.ForeignKey(
        BusinessLocation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="inventory_items",
        help_text="Optional. Null applies business-wide.",
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_items"
    )
    name = models.CharField(max_length=255)
    unit = models.CharField(max_length=32, help_text='e.g. "kg", "each", "liter".')
    # Decimal, not integer — units like kg/liter are fractional. Only ever
    # written directly on create (a starting balance) or by
    # services.adjust_stock thereafter — see InventoryItemSerializer.
    current_quantity = models.DecimalField(
        max_digits=12, decimal_places=3, default=0, validators=[MinValueValidator(Decimal("0"))]
    )
    low_stock_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_item_name_per_business"),
        ]
        ordering = ["business", "name"]

    def __str__(self):
        return f"{self.name} ({self.current_quantity} {self.unit})"


class InventoryTransaction(models.Model):
    """
    Append-only ledger. `save()`/`delete()` below enforce that directly —
    not just "no endpoint exposes editing it" — so the invariant holds
    even against direct ORM/admin/shell access.
    """

    class TransactionType(models.TextChoices):
        RESTOCK = "restock", "Restock"
        USAGE = "usage", "Usage"
        WASTE = "waste", "Waste"
        CORRECTION = "correction", "Correction"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="transactions")
    # Positive for restock/correction-up, negative for usage/waste/correction-down.
    quantity_change = models.DecimalField(max_digits=12, decimal_places=3)
    transaction_type = models.CharField(max_length=16, choices=TransactionType.choices)
    reason = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="inventory_transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def business(self):
        return self.item.business

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise TypeError("InventoryTransaction is append-only; existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("InventoryTransaction is append-only; rows cannot be deleted.")

    def __str__(self):
        return f"{self.item} {self.quantity_change:+} ({self.transaction_type})"
