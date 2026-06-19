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
| `reservations`, `loyalty`, `employees`, `inventory`, `documents`, `marketing`, `settings` | **Placeholders only.** Each `models.py` documents what's planned and which Phase 1 audit findings / Phase 2 architectural decisions it needs to address. No models, views, or URLs yet. |

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
| `generate_confirmation_code()` / `set_confirmation_code` trigger | `Reservation.save()` override (or `pre_save` signal): generate only if empty, retry on collision since the field is unique | Documented in `reservations/models.py`; not implemented until that app is built |
| `calculate_reservation_end_time()` / `set_reservation_end_time` trigger | Same `Reservation.save()` override: `end_time = start_time + timedelta(minutes=duration_minutes)` if not explicitly set | Documented in `reservations/models.py`; not implemented yet |
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

## Security hardening notes (for when each domain is built)

These are commitments made now so they aren't lost by the time the
relevant app is implemented — see each placeholder `models.py` for the
specific note:

- **Loyalty/gift cards/inventory/finance**: balance and status mutations go
  through `transaction.atomic()` + `select_for_update()` service functions.
  No client-trusted balance math.
- **Employee geofencing**: server computes the Haversine distance from
  submitted GPS coordinates; never trust a client-sent "within range"
  boolean.
- **Clock-in/out**: a real server-side state machine (no clock-out without
  an open clock-in, no double clock-in).
- **Public tracking beacon** (`marketing`): real server-side rate limiting
  keyed by IP + script_key (DRF scoped throttling or django-ratelimit), not
  the old client-side localStorage limiter.
- **Guest reservation booking**: table assignment wrapped in
  `transaction.atomic()` + `select_for_update()` on the candidate table
  rows for that date/slot, closing the double-booking race condition that
  existed in the original Edge Function.
- **Stripe webhook**: signature-verified via `STRIPE_WEBHOOK_SECRET`
  (`finance/webhooks.py`), not authenticated via the normal JWT path since
  Stripe can't send one.
