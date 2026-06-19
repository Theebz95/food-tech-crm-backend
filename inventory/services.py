"""
Stock-adjustment service layer.

The actual fix this module exists for (Phase 1 audit finding): the old
useAdjustStock()/useRecordUsage() hooks did a non-atomic two-step write
(update the item's quantity, then insert a ledger row) directly from the
client — a lost-update risk under concurrent requests, same risk class as
the Loyalty/gift-card balance pattern documented in loyalty/models.py.
`adjust_stock` is the only way `InventoryItem.current_quantity` changes
after creation (see InventoryItemSerializer, which blocks writing it on
update) — locks the item row, recomputes the new quantity, rejects the
adjustment outright if it would go negative, and writes the ledger entry
in the same transaction so the two can never disagree.
"""

from django.db import transaction

from .models import InventoryItem, InventoryTransaction


class InsufficientStockError(Exception):
    pass


def adjust_stock(item: InventoryItem, delta, transaction_type, membership, reason="") -> InventoryTransaction:
    with transaction.atomic():
        locked_item = InventoryItem.objects.select_for_update().get(pk=item.pk)
        new_quantity = locked_item.current_quantity + delta
        if new_quantity < 0:
            raise InsufficientStockError(
                f"Adjustment of {delta} would result in negative stock "
                f"({new_quantity}); current quantity is {locked_item.current_quantity}."
            )

        locked_item.current_quantity = new_quantity
        locked_item.save(update_fields=["current_quantity", "updated_at"])

        return InventoryTransaction.objects.create(
            item=locked_item,
            quantity_change=delta,
            transaction_type=transaction_type,
            reason=reason,
            created_by=membership,
        )
