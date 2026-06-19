"""
Public, unauthenticated Marketing routes — see public_views.py for why
these are separate from urls.py (the staff-side, HasBusinessRole routes).
Mounted at the same `/api/public/` prefix as the Reservations domain's
guest routes (config/urls.py) — distinct path prefixes here don't
collide with reservations.public_urls.
"""

from django.urls import path

from .public_views import FormSubmitView, TrackEventView

app_name = "marketing_public"

urlpatterns = [
    path("track/", TrackEventView.as_view(), name="track"),
    path("forms/submit/", FormSubmitView.as_view(), name="form-submit"),
]
