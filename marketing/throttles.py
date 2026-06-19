"""
Custom throttle for the public tracking/form endpoints' per-script_key
rate limit, independent of caller IP.

The per-IP `ScopedRateThrottle` used alongside this one in
public_views.py stops any single IP from flooding the endpoint, but
script_key identifies a *business*, not one visitor — a distributed burst
spread across many IPs (a botnet, or just a lot of real concurrent
visitors) would otherwise stay under each individual IP's cap while still
hammering one business's script_key. This closes that gap the same way
`reservations.throttles.GlobalReservationLookupThrottle` does for the
Reservations domain's confirmation-code lookup, just keyed by script_key
instead of one single fixed bucket.
"""

from rest_framework.throttling import SimpleRateThrottle


class _ScriptKeyRateThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        script_key = request.data.get("script_key") if hasattr(request, "data") else None
        if not script_key:
            # Nothing to bucket by — the per-IP throttle (and then
            # payload/script_key validation) still bounds this request.
            return None
        return self.cache_format % {"scope": self.scope, "ident": script_key}


class TrackEventScriptKeyThrottle(_ScriptKeyRateThrottle):
    scope = "track_event_script_key"


class FormSubmitScriptKeyThrottle(_ScriptKeyRateThrottle):
    scope = "form_submit_script_key"
