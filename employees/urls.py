from django.urls import path

from .views import GeofenceSettingViewSet, TimeEntryViewSet

app_name = "employees"

geofence_setting_list = GeofenceSettingViewSet.as_view({"get": "list", "post": "create"})
geofence_setting_detail = GeofenceSettingViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

time_entry_list = TimeEntryViewSet.as_view({"get": "list"})
time_entry_detail = TimeEntryViewSet.as_view({"get": "retrieve"})
time_entry_clock_in = TimeEntryViewSet.as_view({"post": "clock_in"})
time_entry_clock_out = TimeEntryViewSet.as_view({"post": "clock_out"})
time_entry_break_start = TimeEntryViewSet.as_view({"post": "break_start"})
time_entry_break_end = TimeEntryViewSet.as_view({"post": "break_end"})

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/geofence-settings/",
        geofence_setting_list,
        name="geofence-setting-list",
    ),
    path(
        "businesses/<uuid:business_id>/geofence-settings/<uuid:pk>/",
        geofence_setting_detail,
        name="geofence-setting-detail",
    ),
    path("businesses/<uuid:business_id>/time-entries/", time_entry_list, name="time-entry-list"),
    path("businesses/<uuid:business_id>/time-entries/<uuid:pk>/", time_entry_detail, name="time-entry-detail"),
    path(
        "businesses/<uuid:business_id>/time-entries/clock-in/",
        time_entry_clock_in,
        name="time-entry-clock-in",
    ),
    path(
        "businesses/<uuid:business_id>/time-entries/clock-out/",
        time_entry_clock_out,
        name="time-entry-clock-out",
    ),
    path(
        "businesses/<uuid:business_id>/time-entries/break-start/",
        time_entry_break_start,
        name="time-entry-break-start",
    ),
    path(
        "businesses/<uuid:business_id>/time-entries/break-end/",
        time_entry_break_end,
        name="time-entry-break-end",
    ),
]
