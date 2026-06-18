"""
Loyalty domain (loyalty programs, points accounts/transactions, gift cards)
— not yet built. Deferred to a follow-up session once the core tenancy app
is confirmed working end-to-end.

Every balance mutation (points accrual/redemption, gift card balance
changes) must go through a service-layer function wrapped in
transaction.atomic() + select_for_update() on the account/card row —
never a client-trusted "new balance" value. This replaces the old
non-atomic read-modify-write pattern in useLoyaltyProgram.ts / useGiftCards.ts
(see Phase 1 audit, "Customers & Loyalty & Gift Cards" — both hooks did a
plain fetch-then-update with no locking, a real lost-update risk under
concurrent requests).
"""
