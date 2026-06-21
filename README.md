# Food Tech CRM Backend

Django REST Framework backend for the restaurant CRM, extracted out of a
Supabase-direct-from-frontend architecture. Deployed independently:
**Django on Railway**, frontend (separate repo) on **Vercel**, database
staying on **Supabase Postgres**. The frontend keeps using Supabase Auth
client-side exactly as before; this backend validates the same JWTs rather
than replacing them.

**Migration complete.** Every domain identified in the original Phase 1
audit has been ported, fixed, and tested — `core`, `authentication`,
`customers`, `employees`, `reservations`, `inventory`, `documents`,
`marketing`, `settings`, `finance`, and `loyalty` (including Orders and
gift cards). `loyalty` was the last domain built. See "Project status"
below for what each app covers, and the end of "Loyalty domain" for a
final summary of the audit findings this migration closed out.

## Local setup

```bash
cd food-tech-crm-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: at minimum set DATABASE_URL to a real Postgres (Supabase or
# local), and SUPABASE_JWT_SECRET if you want to test authenticated requests
# against a real Supabase project.

python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/ access
python manage.py runserver
```

You'll also need Redis running locally for Celery (`brew install redis &&
brew services start redis`, or `docker run -p 6379:6379 redis`).

### Running Celery worker + beat

In two additional terminals (with the venv activated):

```bash
celery -A config worker -l info
celery -A config beat -l info
```

The worker executes tasks; beat is the scheduler that enqueues them on a
cron-like schedule (`check_expired_trials`, `expand_recurring_schedules`,
`mark_overdue_invoices`/`mark_overdue_bills`, `expand_recurring_transactions`,
all daily — see `config/settings.py` -> `CELERY_BEAT_SCHEDULE`).

### Stripe webhook (local testing)

```bash
stripe listen --forward-to localhost:8000/api/finance/webhooks/stripe/
```
Copy the `whsec_...` it prints into `STRIPE_WEBHOOK_SECRET` in `.env`.

## Project status

| App | Status |
|---|---|
| `core` | **Built.** `Business`, `BusinessLocation`, `BusinessMembership` models + `HasBusinessRole`/`IsBusinessManager`/`IsBusinessOwner` permission classes + `check_expired_trials` Celery task. |
| `authentication` | **Built.** Custom `User` model (Supabase-id-keyed), `SupabaseAuthentication` DRF auth class, `IsSuperAdmin` permission class. |
| `finance` | **Built — both parts complete.** Invoicing, payments, estimates, the Stripe webhook, bills/bill payments, bank transactions (manual entry only), recurring transactions (Invoice/Bill generation), chart of accounts, and AR/AP aging reports. See "Finance domain" below. |
| `customers` | **Built.** `Customer`, `CustomerProfile`, `CustomerBusinessLink` models + DRF CRUD for `Customer` (`HasBusinessRole`-scoped) + server-side email/phone validation. See "Customers domain" below for the old-table mapping. |
| `employees` | **Built.** Time tracking + geofencing, scheduling (recurring schedules expanded server-side via Celery Beat), shift swaps, time off, and pay stubs. See "Employees domain" below — including the pay stub tax disclaimer. |
| `reservations` | **Built.** Tables, floor plans, business hours, blackout dates, reservation settings, reservations, and a waitlist — staff CRUD (`HasBusinessRole`) plus a separate, intentionally unauthenticated guest-booking flow. See "Reservations domain" below, including the guest-booking permission model. |
| `inventory` | **Built.** Vendors, inventory items, and an append-only transaction ledger — stock changes only ever happen through `adjust_stock()` (`transaction.atomic()` + `select_for_update()`), never a direct quantity write. See "Inventory domain" below. |
| `documents` | **Built.** File metadata + Supabase Storage (S3-compatible) integration, fixing the old upload-orphan risk with a write-ahead `pending` row. See "Documents domain" below for the chosen strategy and why. |
| `marketing` | **Built.** Website tracking (scripts, visitors, page views, events), leads, form submissions, and Google Ads campaign metadata — a public, unauthenticated tracking beacon + form endpoint (server-side rate limited, payload-validated) alongside staff CRUD. See "Marketing domain" below, including the `script_key` threat model. |
| `settings` | **Built.** `BusinessProfile` — logo, contact info, address, default timezone, notification preference toggles. See "Settings domain" below for which old settings tables this covers vs. where the rest already live (Employees, Reservations). |
| `loyalty` | **Built — the final domain.** Orders (including the points-on-creation coupling the audit flagged), loyalty programs/accounts/points with real tier calculation and expiration, and gift cards with server-side QR generation. See "Loyalty domain" below — this completes domain coverage from the original audit. |

## The new tenancy model vs. the old one

**Old (Supabase):** every domain table had a `user_id` column pointing
directly at the business owner's `auth.users.id`. Row Level Security
policies were just `USING (auth.uid() = user_id)`. There was no concept of
a business as its own entity — the owner's user ID *was* the tenant key —
and no way to have staff or multiple locations without giving everyone the
literal owner account.

**New (this backend):**

- **`Business`** is the actual tenant entity — a real row, not just an
  owner's user ID. It carries the subscription/trial lifecycle fields
  (`is_active`, `trial_ends_at`, `trial_expired`, `subscription_status`,
  `stripe_customer_id`, `stripe_subscription_id`) that used to live on
  `profiles`.
- **`BusinessLocation`** is optional — single-location businesses never
  create one. Tables that care about location (shifts, geofencing) can FK
  to it; a null location means "applies business-wide."
- **`BusinessMembership`** replaces the `user_id` column pattern entirely.
  It's a join table: `(business, user, role)`, where `role` is
  `owner` / `manager` / `staff`, optionally scoped to one `location`.
- **Permission checks** changed shape: instead of "does `row.user_id`
  equal `auth.uid()`", it's now "does this user have an active
  `BusinessMembership` on this `Business` with role >= what's required" —
  implemented once in `core/permissions.py` (`HasBusinessRole` and its
  `IsBusinessManager`/`IsBusinessOwner` shortcuts) so every future domain
  app reuses the same check instead of re-implementing row ownership logic.

Every domain table that used to have a direct `user_id` FK (reservations,
customers, employees, invoices, inventory_items, etc.) will instead FK to
`Business` (and optionally `BusinessLocation`) when those apps are built.

## Auth model

One unified path: the frontend keeps using **Supabase Auth** client-side
(no frontend changes needed for this), and every API request carries the
resulting JWT as `Authorization: Bearer <token>`.
`authentication.authentication.SupabaseAuthentication` verifies that JWT
(HS256, `SUPABASE_JWT_SECRET`) and lazily creates/updates a local `User` row
keyed by the JWT's `sub` claim — so Django gets a normal FK target without
owning signup, login, or password storage.

There is **no separate customer-portal auth system**. The old
`customer_portal_accounts` / `portal_sessions` / PBKDF2 password hashing /
custom session-token scheme is gone entirely. Customer/portal users are
just regular Supabase Auth users like everyone else; the `customers` app's
`CustomerProfile` links a `User` to customer-facing data, and
`CustomerBusinessLink` lets one `CustomerProfile` belong to several
`Business`es (replacing `customer_business_relationships` /
`customer_portal_links`). See "Customers domain" below for the full
old-table mapping.

Superadmin is now a single `User.is_superadmin` boolean (checked via
`authentication.permissions.IsSuperAdmin`), replacing the old `user_roles`
table + `is_superadmin()`/`has_role()` Postgres functions — it was the only
role ever checked outside the per-business membership system, so a flag is
sufficient; per-business roles live entirely in `BusinessMembership`.

## Former Postgres triggers/functions → Django equivalents

Pulled directly from the Phase 1 SQL audit of the old `supabase/migrations`:

| Old (Postgres trigger/function) | New (Django) | Status |
|---|---|---|
| `generate_confirmation_code()` / `set_confirmation_code` trigger | `Reservation.save()` override: generates a 6-char uppercase hex code only if empty, retrying on collision (the original trigger never handled that — it just let the unique constraint raise) | **Built** (`reservations/models.py`) |
| `calculate_reservation_end_time()` / `set_reservation_end_time` trigger | Same `Reservation.save()` override: `end_time = start_time + timedelta(minutes=duration_minutes)` if not explicitly set | **Built** (`reservations/models.py`) |
| `is_superadmin(uuid)` / `has_role()` RPC | `User.is_superadmin` boolean + `IsSuperAdmin` DRF permission class | **Built** (`authentication/`) |
| `sync_customer_to_portal_account()` / `sync_customer_to_portal` trigger (kept a linked portal account's name/phone in sync with the customer row) | **Moot** under unified auth — see "Customers domain" below for the full mapping | N/A |
| Invoice/payment/bill status transitions | **No DB trigger existed for these even in the old system** — confirmed by grepping every migration; only the generic `updated_at` trigger touched those tables. All of that math was client-side. Now `finance/services.py`: `transaction.atomic()` + `select_for_update()`, never trusting a client-sent total. | **Built** (`finance/`) |
| Gift card balance, loyalty points accrual | Same old finding (no DB trigger, client-side math). Now `loyalty/services.py`: `transaction.atomic()` + `select_for_update()`, the same way `finance/services.py` does it | **Built** (`loyalty/`) |
| `check_expired_trials()` function + `pg_cron` daily job | `core.tasks.check_expired_trials` Celery Beat task, same three-step logic (expire -> reactivate -> re-expire), against `Business` instead of `profiles` | **Built** |

## Customers domain

Three models, replacing four old tables/concepts — they're kept distinct
because "a business's CRM record for a person" and "that person's portal
login" are different concerns that don't always coincide:

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `customers` (FK'd `user_id` directly to the business owner) | `Customer` (`customers/models.py`) | FK'd to `core.Business` instead, like every other domain table under the new tenancy model. No login required to exist — most `Customer` rows never get one. |
| `customer_portal_accounts` / `portal_sessions` (separate PBKDF2 password + custom session-token auth) | `CustomerProfile` (OneToOne with `authentication.User`) | Gone entirely as a separate auth system — a customer with portal access is just a Supabase Auth `User` like a business user, with a `CustomerProfile` attached for customer-facing fields. Also adds **`email_verified`/`email_verified_at`**, which the old system never tracked at all (Phase 1 audit finding). |
| `customer_business_relationships` / `customer_portal_links` | `CustomerBusinessLink` (join table: `CustomerProfile` ↔ `Business`) | Lets one logged-in customer be linked to multiple businesses — e.g. loyalty across a chain's locations, or several unrelated businesses they're a customer of. |
| `sync_customer_to_portal_account()` trigger | N/A — moot | No longer needed; profile data lives in exactly one place (`CustomerProfile`), so there's nothing to keep in sync. |

`Customer` CRUD is exposed at `api/businesses/<business_id>/customers/`
(`customers/views.py` → `CustomerViewSet`), scoped via
`core.permissions.HasBusinessRole` so only users with an active
`BusinessMembership` on that business can see or modify its customers. The
`business` field on `Customer` is read-only in the API — it's set by the
view from the URL, not accepted from the request body, so a request can't
write into a different tenant by changing a field in the payload. Email
format and phone format (`^\+?[0-9]{7,15}$`) are validated server-side in
`customers/serializers.py`, per the Phase 1 audit finding that this used to
be client-side only and trivially bypassed by calling the API directly.

## Employees domain

There is no separate Employee-as-user model. An "employee" is a
`core.BusinessMembership` with role `staff` or `manager`; every model below
FKs to `BusinessMembership` (directly, or transitively via `EmployeeShift`),
which already identifies both the person (`.user`) and which
`Business`/`BusinessLocation` they belong to.

Covers time tracking + geofencing, scheduling (recurring schedules, shifts,
swaps, time off), and pay stubs.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `geofence_settings` | `GeofenceSetting` (`employees/models.py`) | FK'd to `Business` and optionally `BusinessLocation` (null = business-wide), instead of whatever the old schema used. `enabled=False` (or no row) means geofencing isn't enforced for that scope. |
| `time_tracking` | `TimeEntry` | FK'd to `BusinessMembership` instead of a direct `user_id`. Stores raw client-reported coordinates (`clock_in_lat`/`lng`, `clock_out_lat`/`lng`) separately from the server-computed, audit-only `clock_in_distance_meters`/`clock_in_within_geofence` fields — the client's own opinion of its location is never trusted as the verdict. |
| `time_tracking_breaks` | `TimeEntryBreak` | FK'd to `TimeEntry`. Same one-open-at-a-time state machine as clock-in/out. |
| *(no old equivalent)* | `LocationVerificationLog` | New. Audit trail of every geofence check attempt — including rejected clock-in/out attempts that never produced a `TimeEntry` at all. Without this, a hard-blocked attempt would leave no record anywhere. |

**The actual security fix:** the original frontend (`src/lib/geolocation.ts`)
computed Haversine distance in the browser and trusted the client's own
"within range" boolean — trivially spoofable by anyone who can edit a JS
variable or replay a request with a forged payload. `employees/services.py`
(`haversine_distance_meters`, `verify_geofence`) now computes that distance
server-side from raw lat/lng, for both clock-in and clock-out, and that
computed value — never a client-sent one — is what's stored and what
clock-in/out approval is based on.

**Concurrency:** clock-in locks the relevant `BusinessMembership` row
(`select_for_update()` inside `transaction.atomic()`) before checking for
an existing open `TimeEntry`, since a brand-new clock-in has no `TimeEntry`
row yet to lock. Clock-out and break actions lock the existing open
`TimeEntry` row instead. A DB-level `UniqueConstraint` (one open `TimeEntry`
per membership, one open `TimeEntryBreak` per `TimeEntry`) backs this up as
a second line of defense.

**Geofencing: hard block vs flag.** A clock-in or clock-out outside the
configured radius is currently a hard block — the request is rejected
outright with a 400, no `TimeEntry` is created/updated, and the attempt is
still recorded in `LocationVerificationLog`. This was the simplest correct
behavior to ship the actual security fix now, but it's a real product
decision, not just an engineering one: a hard block means an employee with
bad GPS reception (or a manager who legitimately needs to clock in from
just outside the radius) is locked out entirely, with no way to clock in
until someone fixes the geofence or their location. The alternative —
allow the clock-in but flag it `within_geofence=False` for manager review —
is already fully supported by the data model (every field needed for that
review exists today), it just isn't wired up as a per-business toggle yet.
**This should be revisited deliberately** as a `Business`/`GeofenceSetting`-level
configurable choice once there's product input on which businesses need
which behavior, rather than assumed away. See the `TODO` in
`employees/services.py:clock_in`.

**Endpoints** (all under `core.permissions.HasBusinessRole`, same tenant-scoping pattern as Customers):

- `GET/POST /api/businesses/<business_id>/geofence-settings/` and `GET/PUT/PATCH/DELETE .../<id>/` — manager+ only.
- `GET /api/businesses/<business_id>/time-entries/` and `GET .../<id>/` — read-only list/retrieve.
- `POST /api/businesses/<business_id>/time-entries/clock-in/` and `.../clock-out/` — body: `{"latitude": ..., "longitude": ...}`. Acts on the caller's own membership for that business; there is no way to clock another membership in/out through this API.
- `POST /api/businesses/<business_id>/time-entries/break-start/` and `.../break-end/` — no body; acts on the caller's own currently-open `TimeEntry`.

These are explicit state-transition actions, not generic CRUD — `TimeEntry`
and `TimeEntryBreak` have no create/update/delete endpoint at all, only the
service-layer functions in `employees/services.py` can mutate them.

### Scheduling: positions, shifts, swaps, time off

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `positions` | `Position` | FK'd to `Business`. Carries `hourly_rate` — see the design-choice note in `employees/models.py` for why the rate lives here rather than on `BusinessMembership`. |
| *(no old equivalent)* | `EmployeeAvailability` | New. General weekly availability (day-of-week + time range) per `BusinessMembership`. Not yet matched automatically against schedules. |
| `shift_templates` | `ShiftTemplate` | Reusable shift definition (position, day-of-week, start/end time). One row per day-of-week, same convention as `EmployeeAvailability`. |
| `recurring_schedules` | `RecurringSchedule` | The durable rule ("this membership works this template, weekly"). Always references a `ShiftTemplate`. |
| `employee_shifts` | `EmployeeShift` | **The actual security/data-integrity fix below.** FK'd to `BusinessMembership` + `Position`, with a nullable FK back to the `RecurringSchedule` that generated it (null for one-off manually created shifts). |
| `shift_swap_requests` | `ShiftSwapRequest` | Give-away/reassign model, not a true two-way trade (matches what the old schema actually supported — one `shift`, optional `target_membership`). |
| `time_off_requests` | `TimeOffRequest` | Standard request/approve/reject, FK'd to `BusinessMembership`. |

**The actual fix:** recurring schedules used to be expanded into visible
shifts entirely client-side, on every render (`useRecurringSchedules`-style
logic in the old frontend) — there was never a durable row for "this
employee works next Tuesday" until a browser happened to compute it from
the rule. `employees/scheduling.py` (`expand_active_recurring_schedules`,
run daily by Celery Beat — see `CELERY_BEAT_SCHEDULE` in
`config/settings.py`) now creates real, persisted `EmployeeShift` rows on a
rolling 4-week-ahead basis. It's idempotent: a DB-level partial unique
constraint on `(recurring_schedule, start_at)`
(`EmployeeShift.Meta.constraints`) backs up the `get_or_create()` call, so
re-running the task — including two overlapping Beat runs — never
duplicates shifts.

**Endpoints** (manager+ required for everything except an employee's own
availability/swap-request/time-off-request creation; see
`employees/views.py` for the exact per-action permission split):

- `/api/businesses/<business_id>/positions/`, `/shift-templates/`, `/recurring-schedules/` — manager+ CRUD.
- `/api/businesses/<business_id>/availabilities/` — any business member, scoped to their own rows (managers can see everyone's).
- `/api/businesses/<business_id>/shifts/` — visible to the whole team; create/update/delete and `.../<id>/set-status/` are manager+ only.
- `/api/businesses/<business_id>/shift-swap-requests/` — any member can request a swap on their own shift; `.../<id>/approve/` and `.../reject/` are manager+ only. An open request (no `target_membership`) can be resolved at approval time via `{"target_membership_id": ...}`.
- `/api/businesses/<business_id>/time-off-requests/` — any member can request their own; `.../<id>/approve/` and `.../reject/` are manager+ only.

### Pay stubs

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `pay_stubs` | `PayStub` | FK'd to `BusinessMembership` and the `Position` whose `hourly_rate` was used. `breakdown` (JSONField) stores the full per-week regular/overtime/tax math, not just the final numbers. |

**The actual fix:** gross/net pay used to be computed client-side
(`PayStubs.tsx`) with no overtime or tax logic and no validation at all.
`employees/payroll.py` (`generate_pay_stub`) computes it server-side, pulling
real worked hours from `TimeEntry` (clock_out − clock_in, minus any closed
breaks) — never manual entry. Overtime is split per ISO week against a
threshold read from `Business.extra_settings["overtime_threshold_hours"]`
(default 40), since "40 hrs/week" only makes sense applied week-by-week
even across a biweekly pay period.

**⚠️ Pay stub tax disclaimer:** the tax deduction in `employees/payroll.py`
(`PLACEHOLDER_FLAT_TAX_RATE`) is a single flat percentage of gross pay. It
has **no concept of tax brackets, filing status, jurisdiction, FICA/social
security, or any other real payroll tax rule.** It exists only so `PayStub`
has a structurally complete `net_pay` for development/demo purposes. **Do
not use this for real payroll** without replacing it with real,
jurisdiction-correct tax logic (ideally via a payroll tax provider/API) and
getting accountant/legal sign-off first. This is called out again, loudly,
in that module's docstring.

**Endpoint:** `GET /api/businesses/<business_id>/pay-stubs/` and
`.../<id>/` — staff see only their own, manager+ see everyone's.
`POST .../pay-stubs/generate/` (manager+ only) —
`{"membership_id": ..., "position_id": ..., "pay_period_start": ..., "pay_period_end": ...}`.
Generating a second pay stub for the same membership + period is rejected
(`PayStubAlreadyExistsError`) rather than silently overwriting one.

## Reservations domain

Tables, floor plans, business hours, blackout dates, reservation settings,
reservations, and a waitlist.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `restaurant_tables` | `RestaurantTable` (`reservations/models.py`) | FK'd to `core.BusinessLocation` (required — see below). `position_x`/`position_y` are the only source of truth for where a table sits on the floor plan; nothing else stores position. |
| `floor_plans` | `FloorPlan` | `layout` JSONField only ever references `RestaurantTable` ids + non-positional metadata (rotation/label) — never x/y. See the actual fix below. |
| `business_hours` | `BusinessHours` | FK'd to `BusinessLocation`, one row per day-of-week. See the actual fix below for the enumeration issue this replaces. |
| `blackout_dates` | `BlackoutDate` | FK'd to `BusinessLocation`. Blocks guest booking entirely for that date; checked by both the availability-slot calculation and the booking service. |
| `reservation_settings` | `ReservationSetting` | `OneToOneField(Business)` — one row per business (booking window, buffer, max party size, slot interval, default duration), not per location. Auto-created with defaults on first staff read (`GET .../reservation-settings/`) rather than requiring a separate create step. |
| `reservations` | `Reservation` | No `User` FK — see "Guest booking permission model" below. `table` is nullable until assigned. `end_time` and `confirmation_code` are always computed by `save()`, never client input. |
| `waitlist` | `Waitlist` | FK'd to `BusinessLocation`. Converting an entry to a real `Reservation` (`reservations/services.py:convert_waitlist_entry`) goes through the same locked table-assignment path as a guest booking. |

**Location scoping, a deliberate departure from `GeofenceSetting`:** every
model above requires a `BusinessLocation` — there's no "business-wide"
fallback the way `GeofenceSetting.location` is optional elsewhere in this
codebase. A reservation is always "a table, at one specific location, at
one specific time"; there's no sensible business-wide table or floor plan.
A single-location restaurant simply creates exactly one `BusinessLocation`
row to use this domain.

### Guest booking permission model

This is the one domain in this codebase where a request can be fully
unauthenticated by design: a walk-up guest booking a table has no `User`
row and no `BusinessMembership` — there's nothing for
`core.permissions.HasBusinessRole` to check. Building a fake/shared "guest"
user just to satisfy the membership model would be worse than having no
auth at all (shared credentials, no real accountability), so the guest
flow is a **separate set of views with a separate permission model**,
not a relaxed version of `HasBusinessRole`:

- **Staff-side** (`reservations/views.py`, mounted at `/api/businesses/<business_id>/...`):
  ordinary `HasBusinessRole`-gated `ModelViewSet`s for tables, floor plans,
  business hours, blackout dates, reservation settings, reservations
  (+ `seat`/`cancel`/`no-show`/`complete` actions), and the waitlist
  (+ `convert-to-reservation`). Same tenant-scoping pattern as every other
  domain in this codebase.
- **Guest-side** (`reservations/public_views.py`, mounted at a distinct
  `/api/public/...` prefix in `config/urls.py` so it's unmistakable at the
  routing level which endpoints require no auth): `authentication_classes = []`
  + `permission_classes = [AllowAny]` on every view — the same reasoning as
  `finance/webhooks.py`'s `StripeWebhookView` skipping `SupabaseAuthentication`
  entirely, since a guest request carries no Supabase JWT to even attempt.
  Covers: `GET .../availability/` (open slots for a date/party size),
  `POST .../reservations/` (create a booking), `POST .../waitlist/` (join
  directly), `GET .../business-hours/` (one location's hours), and
  `GET /api/public/reservations/<confirmation_code>/` +
  `.../cancel/` (exact-match lookup/cancel by the code the guest was given
  — there is no list endpoint anywhere in this app, so a guest can't
  enumerate anyone else's reservation).

**Rate limiting is the primary abuse defense** for these endpoints, since
there's no authenticated-user throttle bucket to fall back on. Each guest
view has its own `ScopedRateThrottle` scope (`config/settings.py`
`DEFAULT_THROTTLE_RATES`: `reservation_availability` 30/min,
`reservation_booking` 5/min, `reservation_lookup` 5/min,
`reservation_waitlist` 10/min, `reservation_business_hours` 30/min) rather
than sharing the global `anon` bucket — so a burst against booking can't
also exhaust the budget for the availability check, and vice versa.

The confirmation-code lookup/cancel endpoints (`GuestReservationLookupView`,
`GuestReservationCancelView`) get an extra layer on top, since they're the
one place an attacker can profitably *guess* — a 6-char code is a ~16.7M
keyspace. Both views share one `reservation_lookup` budget (so alternating
GET/POST can't double the effective guess rate) **and** run a second,
IP-independent `GlobalReservationLookupThrottle`
(`reservations/throttles.py`, scope `reservation_lookup_global`, 20/min
total across every client) on top of the per-IP cap — closing the gap
where a guess attempt distributed across many IPs would otherwise stay
under each individual IP's limit. See
`reservations.tests.GuestReservationLookupThrottleTests` for both layers
proven directly (an Nth request within the window gets a real 429, not
just inferred from the throttle class being attached).

`location`/`business` fields on every guest-facing serializer
(`GuestReservationSerializer`, `GuestWaitlistSerializer`) are absent
entirely, not just read-only — the view resolves `location` from the URL
and passes it straight to the booking service, so a payload can't smuggle
a different business's location id into a guest booking the way a
read-only field could still theoretically be probed.

### The actual fixes (Phase 1 audit findings)

1. **Booking concurrency.** The old guest-reservation Edge Function had no
   concurrency control at all — two guests hitting "book" for the same
   table/slot simultaneously could both succeed. `reservations/services.py`
   (`_assign_table_and_book`, used by both `book_reservation` and
   `convert_waitlist_entry`) wraps candidate-table selection + `Reservation`
   creation in `transaction.atomic()` + `select_for_update()` on the
   `RestaurantTable` rows being considered — same pattern as
   `employees/services.py:clock_in` locking the membership row. Proven by
   a real multi-thread test (`reservations.tests.BookingConcurrencyTests`),
   not just inferred from the locking call being present.

2. **`business_hours` enumeration.** The old table was publicly readable in
   a way that let anyone walk through business ids and read every
   business's hours. The guest-facing read endpoint
   (`GuestBusinessHoursView`) only ever resolves hours for the one
   business + location named explicitly in both URL segments — there is no
   route in this app, staff or guest, that lists `BusinessHours` across
   locations or businesses.

3. **Floor plan JSONB drift.** The old `floor_plans.layout` JSONB had no
   schema validation and wasn't kept in sync with table position data, so
   the two could silently drift apart. `FloorPlanSerializer` now validates
   the JSON structure (every `tables[].table_id` must be a real
   `RestaurantTable` for that location, and position keys — `x`/`y`/etc —
   are rejected outright if present in a layout entry), and
   `RestaurantTable.position_x`/`position_y` are the only place position
   data is ever stored. There's nothing left to drift.

4. **Confirmation code / end_time triggers.** Both were Postgres triggers
   (`generate_confirmation_code()`/`set_confirmation_code`,
   `calculate_reservation_end_time()`/`set_reservation_end_time`) — see
   "Former Postgres triggers/functions" above. Now `Reservation.save()`
   logic. The original trigger never handled a confirmation-code collision
   (the unique constraint just raised); `save()` now retries with a fresh
   code instead, proven by `reservations.tests.ConfirmationCodeCollisionTests`
   forcing a collision via a mocked generator.

## Inventory domain

Vendors, inventory items, and a stock-change ledger.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `vendors` | `Vendor` (`inventory/models.py`) | FK'd to `Business`. |
| `inventory_items` | `InventoryItem` | FK'd to `Business` and optionally `BusinessLocation` (null = business-wide — same convention as `GeofenceSetting`, unlike the Reservations domain's required location). `current_quantity`/`low_stock_threshold` are `Decimal`, not integer, since units like kg/liter are fractional. |
| `inventory_transactions` / `inventory_usage` | `InventoryTransaction` | **One unified ledger, not two models** — restock, usage, waste, and manual correction are all "item, quantity_change, who, when," distinguished by `transaction_type`. A separate `InventoryUsage` model would duplicate the same columns/constraints for no behavioral difference; the old system's separate `useAdjustStock()`/`useRecordUsage()` hooks were the same operation under two names. |

**The actual fix (Phase 1 audit finding):** stock-level updates used to be
a non-atomic two-step client write — update the item's quantity, then
insert a ledger row — the same risk class as the Loyalty/gift-card balance
pattern documented in `loyalty/models.py`, just lower stakes.
`inventory/services.py:adjust_stock` is now the *only* way
`InventoryItem.current_quantity` changes after creation: it locks the item
row (`select_for_update()` inside `transaction.atomic()`), recomputes the
new quantity, and **rejects** (raises `InsufficientStockError`) rather
than silently clamps an adjustment that would take stock negative — the
quantity write and the ledger insert happen in the same transaction, so
they can never disagree. Proven by a real multi-thread test
(`inventory.tests.StockAdjustmentConcurrencyTests`: quantity=1, two
concurrent -1 deductions, exactly one succeeds and the other is rejected —
not both succeeding (lost update) and not both being rejected).

`InventoryTransaction` enforces append-only at the model level, not just
by omitting a write endpoint: `save()` raises on any attempt to update an
existing row, and `delete()` always raises (`inventory.tests.LedgerImmutabilityTests`).

`current_quantity` is writable on `InventoryItem` create (setting a
starting balance is establishing a baseline, not logging a change) but
rejected on update (`InventoryItemSerializer.validate`) — every change
after creation must go through `POST .../inventory-items/<id>/adjust-stock/`
so it's always recorded in the ledger.

**Endpoints** (all under `core.permissions.HasBusinessRole`):

- `/api/businesses/<business_id>/vendors/` — CRUD.
- `/api/businesses/<business_id>/inventory-items/` — CRUD (`current_quantity` locked on update, see above). `.../low-stock/` lists items at or below their `low_stock_threshold`. `.../<id>/adjust-stock/` (`{"delta": ..., "transaction_type": ..., "reason": ...}`) is the only way to change quantity post-creation.
- `/api/businesses/<business_id>/inventory-transactions/` — read-only list/retrieve. No create/update/delete route exists; the only way a row gets created is the adjust-stock action above.

## Documents domain

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `documents` | `Document` (`documents/models.py`) | FK'd to `Business`. `storage_key` is the object key in Supabase Storage — server-generated (`{business_id}/{uuid4}-{filename}`), never client-supplied. `status` (`pending`/`uploaded`/`failed`) is the mechanism behind the fix below. |

**The actual fix (Phase 1 audit finding) and the strategy chosen.** The
old flow uploaded straight to Supabase Storage from the browser, then made
a separate call to insert the metadata row — if that second call failed,
the file was already sitting in storage with **no database record of it
at all**, discoverable only by walking the bucket directly.

Two ways to close that were on the table:

- **(a) DB row first, in a `pending` state, then upload, then mark
  `uploaded`.** Chosen.
- (b) Upload first, then a compensating delete of the just-uploaded file
  if the DB insert fails.

(a) wins because the DB row always exists *before* any file does — a
failed upload (`documents/services.py:upload_document`) can only ever
produce a `failed` row pointing at a storage key that was never actually
written. There is no code path where a real file exists with zero
database trace, because every storage key that's ever written is one a
row already pointed at first. (b) requires a compensating delete that can
itself fail (storage unreachable, timeout, whatever caused the original
failure also taking out the cleanup call) — which reintroduces exactly the
orphan risk it's supposed to close, just shifted one step later.

Delete (`documents/services.py:delete_document`) mirrors this: **storage
delete first, then the DB row.** If the storage delete fails, the row
survives — a visible, retryable state — rather than deleting the row
first and risking an untracked file with the record already gone (the
same bug, in reverse). Proven directly, not just inferred from the code's
ordering: `documents.tests.DocumentUploadTests` mocks the storage call to
raise and confirms the result is exactly one `failed` row (not a phantom
file, not a stuck `pending` row, not a crash); `DocumentDeleteTests`
mocks a failing storage delete and confirms the row is still there
afterward.

**Residual case, documented rather than hidden:** if the upload itself
succeeds but the immediately-following "mark `uploaded`" field write
fails (a plain DB write on an existing row — possible but far less likely
than the upload itself failing), the row is left on `pending` while a real
file exists in storage. This is *not* an orphan — the row still exists and
still points at the right key — just an inaccurate status, recoverable by
re-checking storage or re-running the mark-complete step. Far narrower and
more recoverable than the original bug.

**Storage backend:** Supabase Storage's S3-compatible API via `boto3`
(`documents/storage.py`), not Django's storage abstraction
(`django-storages` etc) — `upload_document`/`delete_document` need
precise control over the order of operations above, and a thin set of
plain functions (`upload_file`/`delete_file`/`get_presigned_url`) is
trivially mockable in tests without faking out `FileField`/`Storage`
machinery. Credentials: `SUPABASE_STORAGE_ENDPOINT_URL`/`_BUCKET`/`_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`/`_REGION`
in `.env` (separate from `SUPABASE_SERVICE_ROLE_KEY`) — same
placeholder-via-`.env` pattern as the Postgres connection string.

**Download:** a presigned URL (`GET .../download/` → `{"url": "..."}`),
not proxied through a Django view — this lets Supabase's storage edge
serve the bytes directly instead of tying up a Django worker process
streaming a potentially large file.

**Endpoints** (`core.permissions.HasBusinessRole`):

- `GET /api/businesses/<business_id>/documents/` / `.../<id>/` — list/retrieve.
- `POST /api/businesses/<business_id>/documents/` — multipart upload (`file`, optional `name`); goes through `services.upload_document`, never generic `ModelViewSet.create()`.
- `GET /api/businesses/<business_id>/documents/<id>/download/` — presigned URL; 400 if the document isn't in `uploaded` status.
- `DELETE /api/businesses/<business_id>/documents/<id>/` — goes through `services.delete_document` (storage then row), never generic `ModelViewSet.destroy()`.

## Marketing domain

Website tracking, leads, form submissions, and Google Ads campaign metadata.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `tracking_scripts` (implicit/old Edge Function config) | `TrackingScript` (`marketing/models.py`) | FK'd to `Business`. `script_key` is generated server-side (`secrets.token_urlsafe(32)`) — never client-chosen, never sequential. One business can have more than one (e.g. separate marketing site vs. app subdomain). |
| `website_visitors` / `analytics_sessions` | `WebsiteVisitor` | Anonymous, server-assigned identity — see "Guest-booking-style permission model" below. Carries `is_suspicious`/`flagged_at` for the abuse heuristic. |
| `page_views` | `PageView` | FK'd to `WebsiteVisitor`. |
| `tracking_events` | `TrackingEvent` | FK'd to `WebsiteVisitor`. `event_type` is one of a fixed set (`click`/`scroll`/`form_view`/`outbound_link`/`conversion`/`custom`), never an arbitrary client string. `metadata` is size-capped — see "Payload validation" below. |
| `leads` | `Lead` | FK'd to `Business`. Basic UTM attribution fields + an optional FK to `GoogleAdsCampaign`. De-duped per business by email (mirrors `Customer`'s pattern), which is also what makes `services.submit_form`'s `get_or_create`-by-email safe. |
| `form_submissions` | `FormSubmission` | FK'd to `Business` and optionally `Lead`. `ip_address` is stored but **never serialized by the API** — see "Form submissions" below. |
| `google_ads_campaigns` | `GoogleAdsCampaign` | FK'd to `Business`. OAuth tokens encrypted at rest — see "OAuth token storage" below. |

### The script_key threat model

This is the one domain in this codebase where the public endpoints can't
be protected by anything resembling an authorization check, by
construction. The tracking beacon and form-submission endpoint are called
from arbitrary visitor browsers on a business's own website — there is no
`User`, no `BusinessMembership`, nothing to check. The only thing
identifying the caller is `script_key`, embedded directly in the
`<script>` tag's source on that website. **Anyone who views page source
can read it.** Generating it more carefully (longer, more random, signed,
whatever) does not change this — it is fundamentally visible, and
therefore can never function as a secret. `script_key` answers "which
business is this for," never "is this caller authorized."

Given that, what actually defends `/api/public/track/` and
`/api/public/forms/submit/`:

1. **Server-side rate limiting, not the client-side kind.** The old
   `useRateLimiter.ts` was a localStorage-based limiter — meaningless
   against a real abuser, who simply doesn't run that JS and hits the
   endpoint directly with `curl`/a script. Every limit here is enforced
   server-side, in two independent dimensions (see "Rate limiting" below).
2. **A uniform rejection for every way `script_key` resolution can fail.**
   `services.resolve_script_key` returns `None` for a nonexistent key, an
   inactive (revoked/rotated-away) key, and a malformed key alike, and
   every caller of it (`public_views.py`) returns the exact same
   `{"detail": "Invalid request."}` / 400 regardless of which. Without
   this, an attacker could distinguish "doesn't exist" from "exists but
   disabled" — confirming a guess is close, or that a business exists at
   all — by tweaking inputs and watching the response change. Proven
   directly: `marketing.tests.ScriptKeyRejectionTests` asserts all three
   failure modes produce byte-identical responses. (Payload *shape*
   errors — a missing field, wrong type — still get normal DRF field
   errors; only `script_key` resolution gets the generic response, since
   shape isn't secret but key validity is exactly the thing that must not
   be probeable.)
3. **Visitor identity is never client-supplied.** `WebsiteVisitor` rows are
   identified by a server-set, `httponly` cookie (`ftc_vid`) — the public
   endpoints never read a visitor id out of the request body at all. A
   cookie value that doesn't resolve to a row for *this* business (wrong
   business, tampered, garbage, or simply absent) is always treated as "no
   visitor yet" rather than adopted as-is (`services.get_or_create_visitor`).
   This closes off impersonating another visitor's history or smuggling
   identity across businesses, by construction rather than validation.
   **Known limitation, documented rather than hidden:** the tracking
   domain (this API) differs from the business's own website domain, so
   this cookie is third-party from the browser's perspective — modern
   browsers' third-party-cookie restrictions (Safari ITP, Chrome's
   phase-out) mean cross-session visitor continuity isn't fully reliable
   in every browser. A more invasive fingerprinting approach would trade
   that reliability gap for a privacy one; not attempted here.
4. **Payload validation.** `event_type` is a fixed, validated set, not an
   unbounded string. `metadata`/`form_data` are capped at 4KB/8KB
   respectively (`marketing.serializers.MAX_METADATA_BYTES`/`MAX_FORM_DATA_BYTES`)
   — an unvalidated JSONField on a public endpoint is an open invitation
   to store arbitrarily large payloads.
5. **A bot/abuse heuristic that flags, doesn't block.** If a single
   `WebsiteVisitor` generates 20+ page views/events within 10 seconds
   (`services.HIGH_FREQUENCY_WINDOW`/`HIGH_FREQUENCY_THRESHOLD`),
   `is_suspicious`/`flagged_at` get set. This is a foundation — "this
   traffic looks automated" surfaced to the business — not a full
   bot-detection system; legitimate requests are never rejected because of
   it.

### Rate limiting

Two independent dimensions on every public request, both server-side
(`marketing/throttles.py`, rates in `config/settings.py` `DEFAULT_THROTTLE_RATES`):

| Scope | Rate | Why |
|---|---|---|
| `track_event_ip` | 300/minute per caller IP | A real page load can fire several beacon calls (one pageview + a few interaction events), and many real visitors can legitimately share one corporate/NAT IP — generous enough not to false-positive on normal traffic, while still bounding a single-IP flood. |
| `track_event_script_key` | 6000/minute (100/sec), per business, **independent of IP** | The actual circuit-breaker against volumetric abuse targeting one business. IP-independent on purpose — the same gap as `reservations.throttles.GlobalReservationLookupThrottle`: a burst distributed across many IPs would otherwise stay under each IP's individual cap while still hammering one `script_key`. Generous enough for a genuinely popular site with many concurrent visitors. |
| `form_submit_ip` | 10/minute per caller IP | A real visitor rarely submits a form more than once or twice a minute. |
| `form_submit_script_key` | 200/minute per business, independent of IP | Tighter than the event beacon on both axes — a form submission is consequential (creates a `Lead`); a flood of fake submissions pollutes lead data and costs staff time triaging it, where a flood of fake page views mostly just costs storage. |

`marketing.tests.RateLimitingTests` proves both axes trip a real 429 (per-IP,
per-script_key, and the distributed-burst-across-many-IPs case for both
endpoints) — patching `THROTTLE_RATES` down per-test to make the threshold
reachable quickly, rather than literally sending thousands of requests to
exercise the production numbers above.

### OAuth token storage

`GoogleAdsCampaign.access_token`/`refresh_token` use `EncryptedTextField`
(`marketing/encryption.py`) — Fernet (AES-128-CBC + HMAC, authenticated)
encryption at rest, not a plain `CharField`. A raw DB dump, backup, or
leaked connection string no longer hands over a directly usable OAuth
token. Keyed by a dedicated `FIELD_ENCRYPTION_KEY` setting (`.env`) —
deliberately not reusing `SECRET_KEY` or `SUPABASE_JWT_SECRET`, so
rotating one doesn't entangle the other. Hand-rolled rather than pulling
in `django-cryptography`/`django-fernet-fields`: it's one field on one
model, and `cryptography` (already a dependency) is all it actually needs.

Both token fields are also `write_only` on `GoogleAdsCampaignSerializer` —
accepted on create/update, never returned by the API in any form,
encrypted or decrypted. `marketing.tests.GoogleAdsCampaignEncryptionTests`
proves the DB column itself isn't the plaintext (`SELECT access_token ...`
via a raw cursor, compared against the value passed in) and that the API
never serializes either field back out — not just that `EncryptedTextField`
exists in the model definition.

### Form submissions

Stricter than the tracking beacon end-to-end (see rate limit table above)
since each one is consequential: it becomes a `Lead`
(`services.submit_form`, de-duplicated per business by email) rather than
just a row in an analytics table. `FormSubmission.ip_address` is stored
for abuse investigation only — `FormSubmissionSerializer` omits the field
entirely (not even read-only), so there is no way to retrieve it through
the API at all; it's visible only via Django admin or direct DB access.

### Endpoints

**Public, unauthenticated** (`authentication_classes = []`, same reasoning
as `finance/webhooks.py`'s `StripeWebhookView` — there's no Supabase JWT
to even attempt to verify):

- `POST /api/public/track/` — `{"script_key", "kind": "pageview"|"event", ...}`.
- `POST /api/public/forms/submit/` — `{"script_key", "form_data": {...}}`.

**Staff-side** (`core.permissions.HasBusinessRole`, same tenant-scoping as
every other domain):

- `/api/businesses/<business_id>/tracking-scripts/` — CRUD; `script_key` is always server-generated (create and the `.../regenerate-key/` action), never client-chosen. Revoke via `is_active=False`; rotate via `.../regenerate-key/`.
- `/api/businesses/<business_id>/website-visitors/`, `.../page-views/`, `.../tracking-events/` — read-only; these are system-managed, not staff-edited.
- `/api/businesses/<business_id>/leads/` — CRUD.
- `/api/businesses/<business_id>/form-submissions/` — read-only (`ip_address` never included — see above).
- `/api/businesses/<business_id>/google-ads-campaigns/` — CRUD; `access_token`/`refresh_token` write-only.

## Settings domain

The old `settings`-shaped tables didn't all end up in one place — most of
them turned out to belong to the domain that actually uses them, not a
generic settings bucket. To avoid confusion later about where to look for
a given setting, here's where every one of them actually lives:

| Old (Supabase) | New home | Notes |
|---|---|---|
| `geofence_settings` | `employees.GeofenceSetting` | Clock-in/out radius config — belongs with Employees, not here. |
| `business_hours` | `reservations.BusinessHours` | Belongs with Reservations (it's what availability is computed against), not here. |
| `reservation_settings` | `reservations.ReservationSetting` | Booking window/buffer/party-size policy — belongs with Reservations. |
| `customer_portal_settings` | **Moot.** No separate portal auth system exists under unified auth — see "Customers domain" above. | N/A |
| `break_settings` | Folded into `employees` if/when break-specific policy (beyond the existing open/close break state machine) is needed — nothing in the old schema needed its own table for this beyond what `TimeEntryBreak` already covers. | N/A |
| *(business profile fields, scattered across `profiles`/business tables in the old schema)* | `BusinessProfile` (`settings/models.py`) | **This is what's actually new here:** logo, contact info, address, a business-wide default timezone, and notification preference toggles. |

So this app (`settings`, Django app label `business_settings` — see
`settings/apps.py` for why the label differs from the package name) ends
up covering meaningfully less than its old-table list once suggested.
That's intentional, not a gap: each setting lives with the domain that
reads it, the same way `core.permissions.HasBusinessRole` lives in `core`
rather than being re-implemented per domain.

### BusinessProfile, not a Business field

`BusinessProfile` is its own model (`OneToOneField(Business)`,
auto-created with defaults on first `GET` — same singleton-per-business
pattern as `reservations.ReservationSetting`), not new fields bolted onto
`core.Business`. `core` is deliberately domain-agnostic — every other
domain FKs into it, it never FKs into a domain — and `BusinessProfile`
needs a real FK to `documents.Document` for the logo (next section),
which `core.Business` can't have without inverting that direction.

### Logo upload reuses Documents directly, not a second flow

`BusinessProfile.logo` is a literal `ForeignKey` to `documents.Document` —
not a second copy of `storage_key`/`status`/`content_type` fields.
Uploading or replacing a logo (`settings.services.set_logo`) calls
`documents.services.upload_document` directly; removing one
(`remove_logo`) calls `documents.services.delete_document` directly. This
gets the exact same orphan-prevention guarantee Documents already has —
the DB row exists before any storage write is attempted, so a failed
upload produces a `failed` `Document` row, never a file with no record —
without a second implementation of that state machine to keep in sync
with the first. Proven the same way `documents.tests` proves it for
Documents itself: `settings.tests.LogoUploadTests` mocks the storage call
to fail and confirms there's still no orphan, and separately confirms a
failed *replacement* upload never touches the still-good existing logo
(the new `Document` row is created and attempted first; the old one is
only swapped out, and only then deleted, after the new upload actually
succeeds).

This is the first cross-domain dependency in this codebase (`settings` →
`documents`) — every other domain so far only depends on `core`/`authentication`.
Justified specifically because the alternative (re-implementing the
pending → uploaded/failed flow a second time for one more file field) is
exactly the kind of duplication this fix is supposed to avoid.

### Role gating

Read access (`GET`) is open to any business member
(`core.permissions.HasBusinessRole`'s default STAFF+); updating profile
fields and both logo actions require `IsBusinessManager` (MANAGER+/OWNER).
Staff can see business settings but not change them. `BusinessProfileView`
additionally re-checks business membership inside `_get_business()`
(independent of the permission class), so this view has the unusual
property of two layers that each independently block a cross-tenant
request — `settings.tests` confirms the isolation tests still fail if
*both* are deliberately disabled at once, not just one.

### Endpoints

All under `core.permissions.HasBusinessRole`/`IsBusinessManager`:

- `GET /api/businesses/<business_id>/profile/` — any member; auto-creates with defaults on first access.
- `PATCH`/`PUT /api/businesses/<business_id>/profile/` — manager+; `logo` is read-only here (set only via the actions below).
- `POST /api/businesses/<business_id>/profile/upload-logo/` — manager+; multipart `file` (+ optional `name`).
- `DELETE /api/businesses/<business_id>/profile/logo/` — manager+; clears the logo and deletes the underlying `Document`. A no-op (200, `logo: null`) if none is set.

## Finance domain — complete

Built across two sessions; both parts are done. Part 1: invoicing,
payments, estimates, a minimal chart of accounts, and the Stripe webhook.
Part 2: bills/bill payments, bank transactions (manual entry only — see
below), recurring transactions (Invoice/Bill generation), and AR/AP aging
reports.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `invoices` | `Invoice` (`finance/models.py`) | FK'd to `Business` and `Customer` (`PROTECT` — a customer with invoice history can't be deleted, only deactivated). `invoice_number` is server-generated and unique per business (`InvoiceNumberSequence`, locked/incremented in the same transaction as the invoice — never `Business` itself, to avoid contending with unrelated operations elsewhere that also touch that row). |
| `invoice_line_items` | `InvoiceLineItem` | **No `tax_rate` field** — see "Tax calculation" below for why; the real source applies one tax type to the whole document, not per line. |
| `payments` | `Payment` | FK'd to `Business` and optionally `Invoice` (nullable — a standalone payment not tied to one). Append-only, same enforcement as `inventory.InventoryTransaction` (`save()`/`delete()` raise on an existing row). Refunds/reversals aren't built (would be a compensating entry, not an edit to this row). |
| `estimates` / `estimate_line_items` | `Estimate` / `EstimateLineItem` | Same shape as `Invoice`/`InvoiceLineItem`. `.../convert-to-invoice/` creates a real `Invoice` from one and locks it against re-conversion (`Estimate.status = converted`). |
| `invoice_templates` | `InvoiceTemplate` | `line_item_presets` is plain JSON — it's only ever a pre-fill default; applying one copies its presets into real, validated `InvoiceLineItem` rows on the invoice actually being created. |
| `chart_of_accounts` | `ChartOfAccount` | **Minimal version** — `name`/`code`/`account_type`, just enough for `Invoice.revenue_account`/`Payment.deposit_account`/`Bill.expense_account`/`BillPayment.payment_account` to optionally reference one. Hierarchical accounts, balances, and journal entries were never requested and aren't built. |
| `bills` / `bill_line_items` | `Bill` / `BillLineItem` (`finance/models.py`) | Mirrors `Invoice`/`InvoiceLineItem` exactly — same tax/discount shape (`finance/tax.py`), same server-generated sequential numbering (`BillNumberSequence`, independent counter from `InvoiceNumberSequence`), same no-`tax_rate`-on-line-items reasoning. FK'd to `inventory.Vendor` (`PROTECT`, same reasoning as `Invoice.customer`). |
| `bill_payments` | `BillPayment` | Mirrors `Payment` exactly — append-only, same locked/recomputed-sum/reject-overpayment shape in `services.record_bill_payment`. Deliberately not a new pattern: an owed document + payments against it + paid-when-covered is the same problem either direction. |
| `bank_transactions` | `BankTransaction` | **Manual entry only** — see "Bank transactions" below. |
| `recurring_transactions` | `RecurringTransaction` | Generates `Invoice` or `Bill` rows on a schedule — see "Recurring transactions" below. |
| *(no old equivalent — see below)* | `StripeWebhookEvent`, `InvoiceNumberSequence`, `EstimateNumberSequence`, `BillNumberSequence` | New infrastructure rows, no old-table equivalent. |

### Tax calculation — ported, not approximated

`finance/tax.py` is a direct port of the old frontend's `tax-utils.ts`
(reproduced in full in that module's docstring for reference). Two things
worth being explicit about, since they're the kind of detail that's easy
to silently get wrong when porting:

1. **5 fixed tax options, no province/region lookup at all** — `ZERO`
   (0%), `GST_5` (5%), `HST_15` (15%), `GST_QST_14975` (14.975%), all 4
   ported directly from the original source, plus `QST_9975` (9.975%)
   added afterward on explicit instruction with the exact rate given —
   not guessed, and called out in `finance/tax.py`'s module docstring as
   not part of the original file. There is no per-province rate table
   anywhere in the source being ported. An unrecognized tax type doesn't
   error — it falls back to 0%, exactly matching the original's
   `TAX_OPTIONS.find(...)?.rate || 0` (`get_tax_rate`'s docstring covers
   why this is the actual ported behavior, not a shortcut taken here).
2. **Tax applies once per document, not per line item.** `calculateTotals()`
   takes one `taxType` for the whole invoice/estimate and computes
   `subtotal -> discount -> taxable_amount -> tax -> total` in that exact
   order. `InvoiceLineItem`/`EstimateLineItem` only carry
   `quantity`/`unit_price` — there's no per-line tax rate to port because
   the source never reads one. `discount_type`/`discount_value`/`tax_type`
   live on `Invoice`/`Estimate` themselves.

The one deliberate adaptation: the calculation runs in `Decimal`, not
floating point, and the result is quantized to 2 decimal places. Same
algorithm, same rates, different rounding precision — consistent with how
every other currency value in this codebase is computed (e.g.
`employees/payroll.py`). `finance.tests.TaxCalculationTests` checks the
ported function against hand-computed expected values for each tax
option, a percentage discount, a fixed discount larger than the
subtotal (floors at zero, doesn't go negative), and the unrecognized-type
fallback.

### Invoice/payment state machine

The actual fix (Phase 1 audit finding): status transitions used to be
computed ad hoc, client-side. Now:

- `paid` is set **only** by `finance/services.py:record_payment` —
  inside `transaction.atomic()` with `select_for_update()` on the
  `Invoice` row, it recomputes the real sum of `Payment` rows (never
  trusts a client-sent running total) and marks `paid` only when that sum
  covers the full `total`. **Overpayment is rejected outright** (raises
  `OverpaymentError`), not silently clamped or accepted-with-a-flag —
  same invariant-violation handling as Inventory's negative-stock
  rejection elsewhere in this codebase. Proven with a real multi-thread
  test (`finance.tests.PaymentConcurrencyTests`: invoice total 100, two
  concurrent payments of 80 each — exactly one succeeds, the other is
  rejected, not both succeeding into a 160-paid lost-update).
- `overdue` is set **only** by the daily `finance.tasks.mark_overdue_invoices`
  Celery Beat task, never computed live on read. Only `sent` invoices past
  `due_date` are touched — `draft`, `paid`, `cancelled`, and already-`overdue`
  invoices are left alone.
- `draft -> sent` and any-non-`paid` `-> cancelled` are explicit actions
  (`.../send/`, `.../cancel/`); nothing else writes `status` directly.

### The Stripe webhook — a real functional gap, now closed

This was never a porting task — the old `create-checkout` Edge Function
only ever started a Stripe Checkout session; nothing synced completion,
renewals, or cancellations back into the database at all.
`finance/webhooks.py` now actually implements the four handlers that
existed only as TODO stubs before this session:

| Event | Effect |
|---|---|
| `checkout.session.completed` | Links `stripe_customer_id`/`stripe_subscription_id` onto `Business` (resolved via `client_reference_id` — expected to be set to the Business's UUID when the Checkout Session is created; that creation step is the other half of this integration and isn't built yet, this webhook only consumes what Stripe sends back), sets `subscription_status = "active"`, `is_active = True`. |
| `customer.subscription.updated` | Syncs `subscription_status` from Stripe's reported status (found via `stripe_subscription_id`). |
| `customer.subscription.deleted` | Sets `subscription_status = "canceled"`; sets `is_active = False` unless the business `is_legacy`. |
| `invoice.paid` | **Stripe's own subscription-billing "Invoice"** (the Business paying *us*), a completely different concept from this domain's `finance.Invoice` (the Business billing *their own* customers) — conflating the two would be a real bug, not a naming nitpick. Only ever touches `Business.is_active`/`subscription_status` (covers a `past_due` subscription catching back up); never creates a `finance.Payment` or `finance.Invoice`. |

**Every "can't resolve a Business for this event" path is logged
(`logging.getLogger("finance.webhooks")`, `WARNING`), never a silent
no-op.** A `checkout.session.completed` with a missing/malformed
`client_reference_id`, or any event whose Stripe id doesn't match a
`Business`, still gets recorded as processed (retrying won't fix a
reference that will never resolve) — but it's always logged first, since
that situation is almost always a sign of a real integration problem (a
misconfigured Checkout Session, a stale subscription id, a webhook
pointed at the wrong environment) and silently dropping it would hide
exactly the thing an operator needs to notice. Proven directly
(`finance.tests.StripeWebhookTests`, several `test_*_is_logged` cases)
rather than just asserting the response code.

**Signature verification**: every request is verified against
`STRIPE_WEBHOOK_SECRET` (`stripe.Webhook.construct_event`); malformed or
incorrectly-signed requests get a 400 before any handler runs.

**Idempotency**: Stripe can and will redeliver the same event. `StripeWebhookEvent.event_id`
(Stripe's own `evt_...` id) is the primary key, so inserting one is an
atomic "have I seen this before" check with no check-then-insert race.
That insert and the handler's side effects share **one** `transaction.atomic()`
block — if the handler raises, the whole thing (dedup row included) rolls
back, so a Stripe retry after a real failure is reprocessed cleanly
rather than being permanently skipped as "already seen." Proven directly
(`finance.tests.StripeWebhookTests.test_replayed_event_id_is_a_no_op_not_double_applied`):
the same event delivered twice results in the handler being called
exactly once, not just one `StripeWebhookEvent` row existing.

### Bills and bill payments — mirrored, not reinvented

`Bill`/`BillLineItem`/`BillPayment` are structurally identical to
`Invoice`/`InvoiceLineItem`/`Payment` — same tax/discount calculation,
same sequential-numbering pattern, same locked/atomic
`record_bill_payment` (rejects overpayment outright, exactly like
`record_payment`), same append-only ledger enforcement. This was a
deliberate choice, not laziness: a bill owed to a vendor and an invoice
owed by a customer are mechanically the same problem (a document with a
total, payments against it, paid-when-fully-covered) pointed in opposite
directions. Proven independently for `Bill`, not just inferred from
`Invoice`'s tests — `finance.test_part2.BillPaymentConcurrencyTests` runs
the same two-concurrent-payments-of-80-against-a-100-total race for
`Bill` that `finance.tests.PaymentConcurrencyTests` runs for `Invoice`.

### Bank transactions — manual entry only

`BankTransaction` supports exactly one entry path right now: manual,
staff-created rows via `POST /api/businesses/<business_id>/bank-transactions/`.
There is no bank-feed/Plaid (or similar) integration — `source`
(`manual`/`imported`, default `manual`) and `external_transaction_id`
(blank for every row created today) exist specifically so a future import
job could populate this same model and de-duplicate against re-imports by
`external_transaction_id`, without a schema change when that's built.
Nothing in this session reads from or writes to any external bank API.

Reconciliation (`POST .../bank-transactions/<id>/reconcile/`, body
`{"target_type": "invoice"|"payment"|"bill"|"billpayment", "object_id": ...}`)
links one `BankTransaction` to whichever of those four it actually
matches, via a `GenericForeignKey` (`reconciled_content_type`/`reconciled_object_id`)
gated by an explicit allow-list (`services.RECONCILIATION_MODELS`) — a
bare `GenericForeignKey` doesn't restrict which models can be referenced
on its own, and a bank line should never be reconcilable against an
unrelated model or another business's row (`reconcile_bank_transaction`
checks the target's `business_id` before linking).

### Recurring transactions

The actual fix (Phase 1 audit finding): recurring transaction generation
used to be manually triggered from the frontend
(`useGenerateRecurringTransaction()`), with no real scheduler at all.
`RecurringTransaction` -> `Invoice`/`Bill` expansion
(`finance/recurring.py`, daily via Celery Beat) reuses the exact same
idempotent rolling-window pattern already proven for
`employees.RecurringSchedule` -> `EmployeeShift` — including the literal
date-stepping code, extracted to `core/recurrence.py` specifically so
both domains share one tested implementation instead of two
independently-maintained copies of the same weekly/biweekly/monthly math.
`employees/scheduling.py:occurrence_dates` is now a thin wrapper around
it; `employees/test_scheduling.py` still passes unchanged, proving the
extraction didn't change that domain's behavior.

`RecurringTransaction` carries its own embedded `line_item_presets` +
`tax_type`/`discount_type`/`discount_value` rather than FK'ing to
`InvoiceTemplate`: there's no equivalent "BillTemplate" model, and
FK'ing the invoice case to `InvoiceTemplate` while the bill case had to
embed its own spec anyway would make the two `kind`s of this one model
asymmetric for no real benefit. `kind` (`invoice`/`bill`) determines
whether expansion creates `Invoice` or `Bill` rows, validated against
exactly one of `customer`/`vendor` being set
(`RecurringTransactionSerializer.validate`).

Idempotency works differently from `EmployeeShift`'s plain
`get_or_create()`, because creating an `Invoice`/`Bill` is a multi-step
operation (sequence number, line items, tax calculation) that doesn't fit
a single-insert `defaults` dict. Instead: a cheap pre-check skips
occurrences already generated, and the actual creation is wrapped in
`transaction.atomic()` with the DB's own unique constraint
(`unique_invoice_per_recurring_transaction_occurrence` /
`unique_bill_per_recurring_transaction_occurrence`) as the real
concurrency safety net — a losing concurrent attempt gets `IntegrityError`,
caught and treated as "already handled." Same shape as
`StripeWebhookEvent`'s insert-first idempotency. Proven directly
(`finance.test_part2.RecurringTransactionExpansionTests.test_running_expansion_twice_does_not_duplicate`):
running expansion twice over the same window creates the documents once,
not twice.

### AR/AP aging reports

The actual fix (Phase 1 audit finding): aging reports used to load every
invoice/bill client-side and bucket them in the browser, with no
pagination. `finance/reports.py` computes the bucket aggregation
server-side, from the real unpaid balance (`total - paid_total`, never a
client-sent number) — `current`, `1_30`, `31_60`, `61_90`, `90_plus`,
based on actual days past `due_date`. Only `sent`/`overdue` invoices (or
`received`/`overdue` bills) with a positive remaining balance are
included; `draft`, `paid`, and `cancelled` documents are excluded.

The bucket *summary* (`GET .../reports/ar-aging/` or `.../ap-aging/`) is
small and fixed-size by construction (5 buckets) and is never paginated.
The underlying rows that make up one bucket can be large, though, so the
same endpoint's `?bucket=<key>` mode returns the actual `Invoice`/`Bill`
rows in that one bucket through real DRF pagination
(`AgingReportDetailPagination`, 25/page) — that's the "detail view" the
audit finding's pagination requirement is actually about; the 5-bucket
summary itself was never the thing that needed pagination.
`finance.test_part2.AgingReportTests` checks both the hand-calculated
bucket categorization (known due dates at +5, -10, -45, -75, -120 days)
and that the detail mode is genuinely paginated (`results`/`count` keys
present), not a dump.

### Endpoints

**Staff-side** (`core.permissions.HasBusinessRole`, same tenant-scoping as
every other domain):

- `/api/businesses/<business_id>/accounts/` — CRUD (`ChartOfAccount`).
- `/api/businesses/<business_id>/invoices/` — CRUD (create/update go through `InvoiceWriteSerializer` + services, never a generic `ModelSerializer.save()`); `.../send/`, `.../cancel/`, `.../record-payment/` actions.
- `/api/businesses/<business_id>/payments/` — read-only; the only way one is created is the `record-payment` action above.
- `/api/businesses/<business_id>/estimates/` — CRUD; `.../convert-to-invoice/` action.
- `/api/businesses/<business_id>/invoice-templates/` — CRUD.
- `/api/businesses/<business_id>/bills/` — CRUD; `.../receive/`, `.../cancel/`, `.../record-payment/` actions.
- `/api/businesses/<business_id>/bill-payments/` — read-only.
- `/api/businesses/<business_id>/bank-transactions/` — CRUD; `.../reconcile/` action.
- `/api/businesses/<business_id>/recurring-transactions/` — CRUD.
- `/api/businesses/<business_id>/reports/ar-aging/` and `.../ap-aging/` — bucket summary by default, `?bucket=<key>` for the paginated detail list.

**Public, unauthenticated**: `POST /api/finance/webhooks/stripe/` —
unchanged path from before this session (kept in its own `finance/webhook_urls.py`
specifically so it didn't need to move when the staff-side routes were
added under the usual `/api/businesses/<business_id>/...` convention).

## Loyalty domain — the final domain

Orders, loyalty programs/accounts/points, and gift cards. The original
audit called this the single strongest case for backend-enforced
transactions in the whole system — every balance mutation here reuses the
exact same `transaction.atomic()` + `select_for_update()` pattern already
proven in `inventory.services.adjust_stock`,
`finance.services.record_payment`/`record_bill_payment`, and
Reservations' table-locking, not a new approach invented for this domain.

| Old (Supabase) | New (Django) | Notes |
|---|---|---|
| `orders` / `order_line_items` | `Order` / `OrderLineItem` (`loyalty/models.py`) | Same shape as `Invoice`/`Bill` — tax/discount calculation reuses `finance.tax.calculate_totals` directly, not a second implementation. No `tax_rate` on line items, same reasoning as `InvoiceLineItem`. |
| `loyalty_programs` | `LoyaltyProgram` | `silver_threshold`/`gold_threshold`/`platinum_threshold` are explicit integer fields, not JSON — there are exactly 3, fixed, so a typed field per threshold is simpler and safer than an unvalidated shape. `points_expire_after_days` is optional (null = never expires, the default). |
| `customer_loyalty_accounts` | `CustomerLoyaltyAccount` | `current_tier` is now actually computed (see "Tier calculation" below) — it existed as a field in the old system but nothing ever set it. |
| `points_transactions` | `PointsTransaction` | Append-only, same enforcement as `inventory.InventoryTransaction`. |
| `gift_cards` | `GiftCard` | `code` is server-generated (`secrets.token_urlsafe`), same standard as `marketing.TrackingScript.script_key` — never client-chosen, never sequential. |
| `gift_card_transactions` | `GiftCardTransaction` | Append-only, mirrors `PointsTransaction`. |

### The actual fixes (Phase 1 audit findings)

1. **Points accrual/redemption was entirely client-side, non-atomic.**
   `award_points`/`redeem_points` (`loyalty/services.py`) lock the
   `CustomerLoyaltyAccount` row; `redeem_points` rejects outright — never
   clamps — an amount that would take `available_points` negative.
   Proven with a real multi-thread test
   (`loyalty.tests.PointsRedemptionConcurrencyTests`: 100 available, two
   concurrent redemptions of 80 each — exactly one succeeds).
2. **Gift card balance reloads/redemptions had the same risk.**
   `reload_gift_card`/`redeem_gift_card` mirror the points functions
   exactly; `redeem_gift_card` additionally rejects an expired or inactive
   card. Same concurrency proof, independently
   (`loyalty.tests.GiftCardConcurrencyTests`).
3. **`current_tier` existed as a field but was never computed.** See
   "Tier calculation" below for the exact rule, now enforced inside the
   same atomic block as every `lifetime_points` change.
4. **No expiration enforcement existed on points or gift card balances.**
   See "Points expiration" below for points; `GiftCard.expires_at` is
   checked directly in `redeem_gift_card`.
5. **QR code generation depended on an external service (QRServer.com).**
   Replaced with the `qrcode` library, generated on demand
   (`GET .../gift-cards/<id>/qr-code/`) — see "QR codes" below for why
   this isn't stored as a `Document`.
6. **Orders auto-awarded points non-atomically on creation.**
   `create_order_and_award_points` creates the `Order` (and its line
   items) and awards the resulting points in **one**
   `transaction.atomic()` block — if anything fails, including inside the
   points-awarding step, the order rolls back too. Proven directly, not
   just inferred from the code wrapping both in one function
   (`loyalty.tests.CreateOrderAndAwardPointsAtomicityTests.test_failure_during_points_award_rolls_back_the_order_too`):
   a forced exception between order creation and the points award leaves
   zero `Order`, `OrderLineItem`, or `PointsTransaction` rows — not a
   half-applied state.

### Tier calculation

`current_tier` is computed from `lifetime_points` — which only ever
increases (redemptions reduce `available_points`, never `lifetime_points`)
— compared against `LoyaltyProgram`'s `silver_threshold`/`gold_threshold`/`platinum_threshold`.
Chosen over lifetime *spend* because `lifetime_points` is already an
explicit field in the data model (so the rule needs no second metric to
track), and because a tier based on points earned rather than dollars
spent is insulated from a `LoyaltyProgram.points_per_dollar` rate change
silently reshuffling everyone's tier. Recalculated inside the same atomic
block as every `lifetime_points` change (`award_points` always;
`redeem_points` calls the same recalculation for consistency, though it's
a no-op there since redemption never changes `lifetime_points`) — **a
tier never drops just because points were spent**
(`loyalty.tests.TierCalculationTests.test_tier_does_not_drop_on_redemption`).

### Points expiration

`LoyaltyProgram.points_expire_after_days` is optional, defaulting to
`null` (never expires) — there is no expiration window in the original
data model to port, so a mandatory policy isn't invented here. When set,
each *earning* `PointsTransaction` gets its own `expires_at` at creation;
the daily `loyalty.tasks.expire_points` Celery Beat task finds transactions
past that date and creates a compensating row for each.

**Deliberate simplification, stated plainly rather than hidden:** this
expires *a specific grant*, capped at
`min(original points_change, account.available_points)` — it is not true
multi-grant FIFO lot tracking (knowing precisely which surviving points
came from which grant after partial redemptions across multiple grants).
It guarantees the account never goes negative and never expires more
than that one grant earned; it does not guarantee strict
oldest-points-first consumption order when an account has several grants
at different ages. Building exact multi-grant reconciliation would be
inventing policy complexity beyond what's asked for, not faithfully
reproducing anything — the original system enforced no expiration at
all. Idempotency (so a Beat task running twice, or two overlapping runs,
never double-expires the same grant) works by giving the compensating row
a FK back to the grant it expires
(`PointsTransaction.expired_transaction`), unique-constrained — this also
means the original earn transaction is never mutated, preserving full
append-only immutability even for expiration. Proven directly
(`loyalty.tests.ExpirePointsTests`, including the clamped-partial-redemption
case and running the task twice).

### QR codes

Generated on demand (`loyalty/qr.py`, the `qrcode` library) rather than
stored as a `Document` (the Documents domain's upload pattern, reused
elsewhere in this codebase — e.g. Settings' logo). A QR code is a
deterministic encoding of data already on the `GiftCard` row (its
`code`); storing a rendered copy would only add cache-invalidation risk
for an asset with no independent value of its own, unlike a business
logo, which *is* the canonical asset. Regenerating it is cheap and always
correct.

### Gift card email — the first Resend integration in this codebase

The prompt for this domain assumed an existing Resend pattern in Finance
for invoice emails to reuse; there wasn't one — checked directly (no
`resend`/`send_mail`/`EmailMessage`/SMTP usage anywhere in this repo
before this domain was built). `core/email.py` (`send_email`) is the
first one, calling Resend's REST API directly via `requests` rather than
the `resend` SDK (one POST, one response, not worth a dependency) — built
as shared `core` infrastructure specifically so Finance's invoice-email
flow (still not built) has something to reuse later, the same way the
Stripe webhook was built ahead of the rest of Finance in an earlier
session because it was new infrastructure other code would depend on.

### Endpoints

All under `core.permissions.HasBusinessRole`:

- `/api/businesses/<business_id>/orders/` — list/retrieve/create (`OrderWriteSerializer` + `services.create_order_and_award_points`); `.../cancel/` reverses any awarded points (clamped).
- `/api/businesses/<business_id>/loyalty-programs/` — CRUD.
- `/api/businesses/<business_id>/loyalty-accounts/` — CRUD (balances/tier always read-only); `.../award-points/`, `.../redeem-points/` actions.
- `/api/businesses/<business_id>/points-transactions/` — read-only.
- `/api/businesses/<business_id>/gift-cards/` — CRUD (create via `GiftCardCreateSerializer` + `services.create_gift_card`, balances always read-only); `.../reload/`, `.../redeem/`, `.../send/` (email), `.../qr-code/` (PNG image response) actions.
- `/api/businesses/<business_id>/gift-card-transactions/` — read-only.

### Migration complete

This closes out every domain identified in the original Phase 1 audit.
Summary of what changed, end to end, across the whole migration:

- **Tenancy model**: a real `Business`/`BusinessLocation`/`BusinessMembership`
  structure replacing the old direct-`user_id`-column-per-table pattern,
  with one shared permission class (`HasBusinessRole`) instead of
  per-table RLS policies.
- **Every client-side, non-atomic balance/total/status computation** found
  in the audit — geofencing, time tracking, payroll, reservation
  double-booking, inventory stock, invoice/bill totals and payment
  status, loyalty points, gift card balances — now goes through a
  server-side service function wrapped in `transaction.atomic()` +
  `select_for_update()`, never trusting a client-sent number.
- **Every missing piece of backend enforcement** the audit flagged as a
  real functional gap, not a porting task — the Stripe webhook, AR/AP
  aging aggregation, recurring schedule/transaction expansion, points/gift-card
  expiration — is now real, scheduled, idempotent infrastructure.
- **Every external-service dependency the audit flagged for removal** —
  QRServer.com (replaced with server-side `qrcode` generation) — is gone.
- **Tenant isolation is proven, not assumed**, for every domain: each
  one's deny-path tests were validated by deliberately breaking
  `HasBusinessRole` (or the relevant guard) and confirming the tests
  failed, then restoring it — the same method, applied consistently from
  the very first domain (`employees`) through the last (`loyalty`).

## Security hardening notes (for when each domain is built)

These are commitments made now so they aren't lost by the time the
relevant app is implemented — see each placeholder `models.py` for the
specific note:

- **Loyalty points / gift cards**: **Built.** `loyalty/services.py`:
  `award_points`/`redeem_points`/`reload_gift_card`/`redeem_gift_card`,
  all `transaction.atomic()` + `select_for_update()`, all rejecting
  outright (never clamping) an operation that would take a balance
  negative. No client-trusted balance math. See "Loyalty domain" above.
- **Inventory stock adjustments**: **Built.** Every quantity change goes
  through `inventory/services.py:adjust_stock` (`transaction.atomic()` +
  `select_for_update()` on the `InventoryItem` row), which rejects an
  adjustment that would take stock negative instead of clamping it. See
  "Inventory domain" above.
- **Document upload orphan risk**: **Built.** DB row written first, in a
  `pending` state, before the storage upload is attempted — a failed
  upload can only ever produce a `failed` row, never a real file with no
  database record. See "Documents domain" above for the full reasoning
  and the alternative considered.
- **Employee geofencing**: **Built.** Server computes the Haversine distance
  from submitted GPS coordinates server-side; never trusts a client-sent
  "within range" boolean. See "Employees domain" above.
- **Clock-in/out**: **Built.** A real server-side state machine (no
  clock-out without an open clock-in, no double clock-in), backed by
  `select_for_update()` + a DB-level unique constraint.
- **Recurring schedule expansion**: **Built.** Persisted into real
  `EmployeeShift` rows by a daily, idempotent Celery Beat task instead of
  being computed client-side on every render. See "Employees domain" above.
- **Pay stub calculation**: **Built**, with a deliberate caveat — gross/net
  pay is computed server-side from real `TimeEntry` hours, but the tax
  deduction is an explicit placeholder, not compliant payroll tax logic.
  See the pay stub tax disclaimer in "Employees domain" above before this
  is used for anything beyond development/demo.
- **Public tracking beacon / form submission** (`marketing`): **Built.**
  Real server-side rate limiting, two independent dimensions (per-IP and
  per-`script_key`, the latter IP-independent), not the old client-side
  localStorage limiter. `script_key` is treated as identifying *which
  business*, never *who's authorized*, since it's visible in client-side
  JS source by necessity. See "Marketing domain" above for the full
  threat model and the chosen rate numbers.
- **Guest reservation booking**: **Built.** Table assignment wrapped in
  `transaction.atomic()` + `select_for_update()` on the candidate table
  rows for that date/slot, closing the double-booking race condition that
  existed in the original Edge Function. See "Reservations domain" above,
  including the guest-booking permission model (separate from
  `HasBusinessRole`, rate-limiting as the primary abuse defense) and the
  `business_hours`-enumeration and floor-plan-drift fixes.
- **Stripe webhook**: **Built.** Signature-verified via `STRIPE_WEBHOOK_SECRET`
  (`finance/webhooks.py`), not authenticated via the normal JWT path since
  Stripe can't send one. All four handlers (`checkout.session.completed`,
  `invoice.paid`, `customer.subscription.updated`/`.deleted`) are now
  actually implemented, not TODO stubs, and event processing is
  idempotent (`StripeWebhookEvent`) — this was a real functional gap in
  the old system, not a porting task. See "Finance domain" above.
