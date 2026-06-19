"""
Extra throttle layer for the guest confirmation-code lookup/cancel
endpoints specifically.

A 6-char confirmation code (Reservation.save()) has a ~16.7M keyspace.
Per-IP rate limiting (ScopedRateThrottle, scope="reservation_lookup")
stops any single IP from brute-forcing it, but a distributed attempt
spread across many IPs/botnet nodes would each stay comfortably under
that per-IP limit while the platform-wide guess rate stays high.
GlobalReservationLookupThrottle adds a second, IP-independent layer: one
shared bucket across every client, capping the *total* guess rate against
these two endpoints regardless of how many distinct IPs it's spread
across. Both throttles run on every request (see public_views.py); either
one tripping is enough to reject the request.
"""

from rest_framework.throttling import SimpleRateThrottle


class GlobalReservationLookupThrottle(SimpleRateThrottle):
    scope = "reservation_lookup_global"

    def get_cache_key(self, request, view):
        # Deliberately ignores the caller's identity/IP — every client
        # shares this one bucket.
        return self.cache_format % {"scope": self.scope, "ident": "all"}
