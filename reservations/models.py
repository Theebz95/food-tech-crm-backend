"""
Reservations domain (reservations, waitlist, blackout dates, floor plans,
restaurant tables) — not yet built. Deferred to a follow-up session once
the core tenancy app is confirmed working end-to-end.

Two pieces of former Postgres trigger logic (see Phase 1 SQL audit) need to
become application code instead of DB triggers when the Reservation model
is built:

  1. confirmation_code generation (was: generate_confirmation_code() /
     set_confirmation_code trigger) -> generate in a Reservation.save()
     override (or pre_save signal), only when the field is empty, e.g.
     `secrets.token_hex(3).upper()`. Should retry on collision since the
     field is unique — the original trigger never handled that case.

  2. end_time calculation (was: calculate_reservation_end_time() /
     set_reservation_end_time trigger) -> compute in the same save()
     override: `self.end_time = self.start_time + timedelta(minutes=self.duration_minutes)`
     when end_time isn't explicitly set.

The public, unauthenticated guest-booking flow (replacing the old
guest-reservation Edge Function) must wrap table assignment in
transaction.atomic() + select_for_update() on the candidate
RestaurantTable rows for that date/time-slot, to close the double-booking
race condition that existed in the original.
"""
