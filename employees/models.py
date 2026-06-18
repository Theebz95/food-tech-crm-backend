"""
Employees / Scheduling / Time Tracking domain — not yet built. Deferred to
a follow-up session once the core tenancy app is confirmed working
end-to-end.

Two pieces of client-trusted logic from the original frontend must become
server-enforced here:

  1. Geofence distance verification: the server computes Haversine distance
     from submitted GPS coordinates against the assigned BusinessLocation's
     configured radius — never trust a client-sent "within range" boolean.
     The original frontend (src/lib/geolocation.ts) computed and trusted
     this entirely client-side.

  2. Clock-in/out state machine: enforce valid transitions server-side (no
     clock-out without an open clock-in, no double clock-in, no
     lunch/break actions outside an active clocked-in session) instead of
     trusting whatever sequence of mutations the client happens to send.

Pay stub calculation (gross/net pay) should also move into a service-layer
function here rather than being computed in a UI form, per the Phase 1
audit finding that PayStubs.tsx did this math client-side with no
validation.
"""
