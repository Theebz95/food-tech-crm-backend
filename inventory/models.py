"""
Inventory & Vendors domain — not yet built. Deferred to a follow-up
session once the core tenancy app is confirmed working end-to-end.

Stock-level mutations (adjustments, usage recording) should go through a
service-layer function wrapped in transaction.atomic() + select_for_update()
on the InventoryItem row, same reasoning as the Loyalty domain: the
original useAdjustStock()/useRecordUsage() hooks did a non-atomic two-step
read-then-write (update item quantity + insert a ledger row) directly from
the client.
"""
