"""
Public, unauthenticated tracking + form-submission endpoints.

`script_key` identifies *which business*, never *who's authorized* — it's
embedded in client-side JS source on the business's own website, so any
visitor's browser (or anyone who reads the page source) can read it. It
is NOT a secret and must never be treated as one. The actual defenses
here are: server-side rate limiting (per-IP and per-script_key — see
`marketing/throttles.py`), strict payload validation (capped JSON sizes,
a known `event_type` set), a single uniform rejection response for every
way `script_key` resolution can fail (so nonexistent/inactive/malformed
are indistinguishable), and treating every incoming event as low-trust —
visitor identity is always server-assigned via cookie, never the client's
own claimed id. See README "Marketing domain" for the full threat model.

`authentication_classes = []` on every view here, same reasoning as
`finance/webhooks.py`'s `StripeWebhookView` and the Reservations guest
endpoints: there's no Supabase JWT to even attempt to verify.
"""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .serializers import FormSubmitSerializer, TrackEventSerializer
from .throttles import FormSubmitScriptKeyThrottle, TrackEventScriptKeyThrottle

# One single response for every script_key-resolution failure
# (nonexistent, inactive, or garbage-but-well-formed) — see module
# docstring. Deliberately not derived from the validation error, so
# there is nothing in the response that varies with *why* it failed.
_INVALID_KEY_RESPONSE = {"detail": "Invalid request."}


def _get_client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class TrackEventView(APIView):
    """POST /api/public/track/ — body: {"script_key", "kind": "pageview"|"event", ...}."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Per-IP (ScopedRateThrottle, keys by caller IP) AND per-script_key
    # (TrackEventScriptKeyThrottle, IP-independent) — both run on every
    # request; either tripping rejects it. See README "Marketing domain"
    # for the chosen rates and reasoning.
    throttle_classes = [ScopedRateThrottle, TrackEventScriptKeyThrottle]
    throttle_scope = "track_event_ip"

    def post(self, request):
        serializer = TrackEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        script = services.resolve_script_key(data["script_key"])
        if script is None:
            return Response(_INVALID_KEY_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        cookie_visitor_id = request.COOKIES.get(services.VISITOR_COOKIE_NAME)
        visitor = services.get_or_create_visitor(script.business, cookie_visitor_id)

        if data["kind"] == "pageview":
            services.record_pageview(visitor, data["url"], data["referrer"])
        else:
            services.record_event(visitor, data["event_type"], data["metadata"])

        response = Response(status=status.HTTP_202_ACCEPTED)
        response.set_cookie(
            services.VISITOR_COOKIE_NAME,
            str(visitor.id),
            max_age=services.VISITOR_COOKIE_MAX_AGE,
            httponly=True,
            samesite="None",
            secure=True,
        )
        return response


class FormSubmitView(APIView):
    """POST /api/public/forms/submit/ — body: {"script_key", "form_data": {...}}."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, FormSubmitScriptKeyThrottle]
    throttle_scope = "form_submit_ip"

    def post(self, request):
        serializer = FormSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        script = services.resolve_script_key(data["script_key"])
        if script is None:
            return Response(_INVALID_KEY_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        services.submit_form(script.business, data["form_data"], _get_client_ip(request))
        return Response(status=status.HTTP_202_ACCEPTED)
