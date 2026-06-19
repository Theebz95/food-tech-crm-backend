"""
Public, unauthenticated Reservations routes — see public_views.py for why
these are intentionally separate from urls.py (the staff-side, HasBusinessRole
routes). Mounted at a distinct `/api/public/` prefix in config/urls.py so
it's unmistakable at the routing level which endpoints require no auth.
"""

from django.urls import path

from .public_views import (
    GuestAvailabilityView,
    GuestBusinessHoursView,
    GuestReservationCancelView,
    GuestReservationCreateView,
    GuestReservationLookupView,
    GuestWaitlistJoinView,
)

app_name = "reservations_public"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/locations/<uuid:location_id>/availability/",
        GuestAvailabilityView.as_view(),
        name="availability",
    ),
    path(
        "businesses/<uuid:business_id>/locations/<uuid:location_id>/reservations/",
        GuestReservationCreateView.as_view(),
        name="reservation-create",
    ),
    path(
        "businesses/<uuid:business_id>/locations/<uuid:location_id>/waitlist/",
        GuestWaitlistJoinView.as_view(),
        name="waitlist-join",
    ),
    path(
        "businesses/<uuid:business_id>/locations/<uuid:location_id>/business-hours/",
        GuestBusinessHoursView.as_view(),
        name="business-hours",
    ),
    path("reservations/<str:confirmation_code>/", GuestReservationLookupView.as_view(), name="reservation-lookup"),
    path(
        "reservations/<str:confirmation_code>/cancel/",
        GuestReservationCancelView.as_view(),
        name="reservation-cancel",
    ),
]
