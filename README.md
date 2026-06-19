# Food Tech CRM Backend

Django REST Framework backend for the restaurant CRM, extracted out of a
Supabase-direct-from-frontend architecture. Deployed independently:
**Django on Railway**, frontend (separate repo) on **Vercel**, database
staying on **Supabase Postgres**. The frontend keeps using Supabase Auth
client-side exactly as before; this backend validates the same JWTs rather
than replacing them.

This is a foundation-only scaffold. Only the `core` app (tenancy model) and
the cross-cutting auth/webhook infrastructure are fully built. Every other
app is a structural placeholder with a documented plan in its `models.py`,
to be built out one domain at a time in follow-up sessions. See "Project
status" below for exactly what exists today.

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
cron-like schedule (currently: `check_expired_trials` daily at 00:00 UTC,
and the `generate_due_recurring_transactions` stub at 01:00 UTC — see
`config/settings.py` -> `CELERY_BEAT_SCHEDULE`).

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
| `finance` | **Partially built.** Stripe webhook endpoint (`webhooks.py`) and a stub `generate_due_recurring_transactions` task — both exist ahead of the domain models because they're new infrastructure / already wired into Celery Beat. No Finance models yet. |
| `customers` | **Built.** `Customer`, `CustomerProfile`, `CustomerBusinessLink` models + DRF CRUD for `Customer` (`HasBusinessRole`-scoped) + server-side email/phone validation. See "Customers domain" below for the old-table mapping. |
| `employees` | **Built.** Time tracking + geofencing, scheduling (recurring schedules expanded server-side via Celery Beat), shift swaps, time off, and pay stubs. See "Employees domain" below — including the pay stub tax disclaimer. |
| `reservations` | **Built.** Tables, floor plans, business hours, blackout dates, reservation settings, reservations, and a waitlist — staff CRUD (`HasBusinessRole`) plus a separate, intentionally unauthenticated guest-booking flow. See "Reservations domain" below, including the guest-booking permission model. |
| `inventory` | **Built.** Vendors, inventory items, and an append-only transaction ledger — stock changes only ever happen through `adjust_stock()` (`transaction.atomic()` + `select_for_update()`), never a direct quantity write. See "Inventory domain" below. |
| `documents` | **Built.** File metadata + Supabase Storage (S3-compatible) integration, fixing the old upload-orphan risk with a write-ahead `pending` row. See "Documents domain" below for the chosen strategy and why. |
| `marketing` | **Built.** Website tracking (scripts, visitors, page views, events), leads, form submissions, and Google Ads campaign metadata — a public, unauthenticated tracking beacon + form endpoint (server-side rate limited, payload-validated) alongside staff CRUD. See "Marketing domain" below, including the `script_key` threat model. |
| `loyalty`, `settings` | **Placeholders only.** Each `models.py` documents what's planned and which Phase 1 audit findings / Phase 2 architectural decisions it needs to address. No models, views, or URLs yet. |

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
| Gift card balance, loyalty points accrual, invoice/payment status transitions | **No DB trigger existed for these even in the old system** — confirmed by grepping every migration; only the generic `updated_at` trigger touched those tables. All of that math was client-side. In this backend it becomes service-layer functions wrapped in `transaction.atomic()` + `select_for_update()` (see `loyalty/models.py`, `finance/models.py`) | Not implemented until those apps are built |
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

## Security hardening notes (for when each domain is built)

These are commitments made now so they aren't lost by the time the
relevant app is implemented — see each placeholder `models.py` for the
specific note:

- **Loyalty/gift cards/finance**: balance and status mutations go
  through `transaction.atomic()` + `select_for_update()` service functions.
  No client-trusted balance math.
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
- **Stripe webhook**: signature-verified via `STRIPE_WEBHOOK_SECRET`
  (`finance/webhooks.py`), not authenticated via the normal JWT path since
  Stripe can't send one.
