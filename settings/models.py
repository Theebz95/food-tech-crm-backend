"""
Business settings domain (geofence_settings, customer_portal_settings,
business_hours, break_settings, reservation_settings, etc. from the old
schema) — not yet built. Deferred to a follow-up session once the core
tenancy app is confirmed working end-to-end.

Most of these were one-row-per-business (or one-row-per-day) settings
tables in the old schema; several are natural candidates to fold into
Business.extra_settings (core app) or BusinessLocation.hours instead of
staying as separate tables — worth deciding per-setting when this app is
built rather than assuming a 1:1 table port.
"""
