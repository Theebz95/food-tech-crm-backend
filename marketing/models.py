"""
Marketing / website tracking domain — not yet built. Deferred to a
follow-up session once the core tenancy app is confirmed working
end-to-end.

The public tracking beacon endpoint (replacing the old `track` Supabase
Edge Function) must implement real server-side rate limiting keyed by
IP + script_key (e.g. DRF scoped throttling or django-ratelimit), replacing
the client-side, localStorage-based limiter (useRateLimiter.ts) that a
public, unauthenticated endpoint could simply ignore by not running that
JS at all.
"""
