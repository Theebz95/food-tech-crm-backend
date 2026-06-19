from django.urls import path

from .views import (
    BlackoutDateViewSet,
    BusinessHoursViewSet,
    FloorPlanViewSet,
    ReservationSettingView,
    ReservationViewSet,
    RestaurantTableViewSet,
    WaitlistViewSet,
)

app_name = "reservations"

table_list = RestaurantTableViewSet.as_view({"get": "list", "post": "create"})
table_detail = RestaurantTableViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

floor_plan_list = FloorPlanViewSet.as_view({"get": "list", "post": "create"})
floor_plan_detail = FloorPlanViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

business_hours_list = BusinessHoursViewSet.as_view({"get": "list", "post": "create"})
business_hours_detail = BusinessHoursViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

blackout_date_list = BlackoutDateViewSet.as_view({"get": "list", "post": "create"})
blackout_date_detail = BlackoutDateViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

reservation_list = ReservationViewSet.as_view({"get": "list", "post": "create"})
reservation_detail = ReservationViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
reservation_seat = ReservationViewSet.as_view({"post": "seat"})
reservation_cancel = ReservationViewSet.as_view({"post": "cancel"})
reservation_no_show = ReservationViewSet.as_view({"post": "no_show"})
reservation_complete = ReservationViewSet.as_view({"post": "complete"})

waitlist_list = WaitlistViewSet.as_view({"get": "list", "post": "create"})
waitlist_detail = WaitlistViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
waitlist_convert = WaitlistViewSet.as_view({"post": "convert_to_reservation"})

urlpatterns = [
    path("businesses/<uuid:business_id>/tables/", table_list, name="table-list"),
    path("businesses/<uuid:business_id>/tables/<uuid:pk>/", table_detail, name="table-detail"),
    path("businesses/<uuid:business_id>/floor-plans/", floor_plan_list, name="floor-plan-list"),
    path("businesses/<uuid:business_id>/floor-plans/<uuid:pk>/", floor_plan_detail, name="floor-plan-detail"),
    path("businesses/<uuid:business_id>/business-hours/", business_hours_list, name="business-hours-list"),
    path(
        "businesses/<uuid:business_id>/business-hours/<uuid:pk>/",
        business_hours_detail,
        name="business-hours-detail",
    ),
    path("businesses/<uuid:business_id>/blackout-dates/", blackout_date_list, name="blackout-date-list"),
    path(
        "businesses/<uuid:business_id>/blackout-dates/<uuid:pk>/", blackout_date_detail, name="blackout-date-detail"
    ),
    path(
        "businesses/<uuid:business_id>/reservation-settings/",
        ReservationSettingView.as_view(),
        name="reservation-settings",
    ),
    path("businesses/<uuid:business_id>/reservations/", reservation_list, name="reservation-list"),
    path("businesses/<uuid:business_id>/reservations/<uuid:pk>/", reservation_detail, name="reservation-detail"),
    path(
        "businesses/<uuid:business_id>/reservations/<uuid:pk>/seat/", reservation_seat, name="reservation-seat"
    ),
    path(
        "businesses/<uuid:business_id>/reservations/<uuid:pk>/cancel/",
        reservation_cancel,
        name="reservation-cancel",
    ),
    path(
        "businesses/<uuid:business_id>/reservations/<uuid:pk>/no-show/",
        reservation_no_show,
        name="reservation-no-show",
    ),
    path(
        "businesses/<uuid:business_id>/reservations/<uuid:pk>/complete/",
        reservation_complete,
        name="reservation-complete",
    ),
    path("businesses/<uuid:business_id>/waitlist/", waitlist_list, name="waitlist-list"),
    path("businesses/<uuid:business_id>/waitlist/<uuid:pk>/", waitlist_detail, name="waitlist-detail"),
    path(
        "businesses/<uuid:business_id>/waitlist/<uuid:pk>/convert-to-reservation/",
        waitlist_convert,
        name="waitlist-convert",
    ),
]
