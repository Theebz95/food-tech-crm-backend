"""
Documents domain — not yet built. Deferred to a follow-up session once the
core tenancy app is confirmed working end-to-end.

File upload should be a single atomic operation (accept the file, write it
to backend-managed storage, insert the metadata row, all within one
request/transaction) — replacing the old two-step client flow (upload to
Supabase Storage directly from the browser, then a separate insert call)
that could orphan files if the second step failed.
"""
