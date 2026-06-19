from django.urls import path

from .views import (
    EmployeeAvailabilityViewSet,
    EmployeeShiftViewSet,
    GeofenceSettingViewSet,
    PayStubViewSet,
    PositionViewSet,
    RecurringScheduleViewSet,
    ShiftSwapRequestViewSet,
    ShiftTemplateViewSet,
    TimeEntryViewSet,
    TimeOffRequestViewSet,
)

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

position_list = PositionViewSet.as_view({"get": "list", "post": "create"})
position_detail = PositionViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

availability_list = EmployeeAvailabilityViewSet.as_view({"get": "list", "post": "create"})
availability_detail = EmployeeAvailabilityViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

shift_template_list = ShiftTemplateViewSet.as_view({"get": "list", "post": "create"})
shift_template_detail = ShiftTemplateViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

recurring_schedule_list = RecurringScheduleViewSet.as_view({"get": "list", "post": "create"})
recurring_schedule_detail = RecurringScheduleViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

shift_list = EmployeeShiftViewSet.as_view({"get": "list", "post": "create"})
shift_detail = EmployeeShiftViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
shift_set_status = EmployeeShiftViewSet.as_view({"post": "set_status"})

swap_request_list = ShiftSwapRequestViewSet.as_view({"get": "list", "post": "create"})
swap_request_detail = ShiftSwapRequestViewSet.as_view({"get": "retrieve"})
swap_request_approve = ShiftSwapRequestViewSet.as_view({"post": "approve"})
swap_request_reject = ShiftSwapRequestViewSet.as_view({"post": "reject"})

time_off_list = TimeOffRequestViewSet.as_view({"get": "list", "post": "create"})
time_off_detail = TimeOffRequestViewSet.as_view({"get": "retrieve"})
time_off_approve = TimeOffRequestViewSet.as_view({"post": "approve"})
time_off_reject = TimeOffRequestViewSet.as_view({"post": "reject"})

pay_stub_list = PayStubViewSet.as_view({"get": "list"})
pay_stub_detail = PayStubViewSet.as_view({"get": "retrieve"})
pay_stub_generate = PayStubViewSet.as_view({"post": "generate"})

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
    path("businesses/<uuid:business_id>/positions/", position_list, name="position-list"),
    path("businesses/<uuid:business_id>/positions/<uuid:pk>/", position_detail, name="position-detail"),
    path(
        "businesses/<uuid:business_id>/availabilities/", availability_list, name="availability-list"
    ),
    path(
        "businesses/<uuid:business_id>/availabilities/<uuid:pk>/",
        availability_detail,
        name="availability-detail",
    ),
    path(
        "businesses/<uuid:business_id>/shift-templates/", shift_template_list, name="shift-template-list"
    ),
    path(
        "businesses/<uuid:business_id>/shift-templates/<uuid:pk>/",
        shift_template_detail,
        name="shift-template-detail",
    ),
    path(
        "businesses/<uuid:business_id>/recurring-schedules/",
        recurring_schedule_list,
        name="recurring-schedule-list",
    ),
    path(
        "businesses/<uuid:business_id>/recurring-schedules/<uuid:pk>/",
        recurring_schedule_detail,
        name="recurring-schedule-detail",
    ),
    path("businesses/<uuid:business_id>/shifts/", shift_list, name="shift-list"),
    path("businesses/<uuid:business_id>/shifts/<uuid:pk>/", shift_detail, name="shift-detail"),
    path(
        "businesses/<uuid:business_id>/shifts/<uuid:pk>/set-status/",
        shift_set_status,
        name="shift-set-status",
    ),
    path(
        "businesses/<uuid:business_id>/shift-swap-requests/",
        swap_request_list,
        name="shift-swap-request-list",
    ),
    path(
        "businesses/<uuid:business_id>/shift-swap-requests/<uuid:pk>/",
        swap_request_detail,
        name="shift-swap-request-detail",
    ),
    path(
        "businesses/<uuid:business_id>/shift-swap-requests/<uuid:pk>/approve/",
        swap_request_approve,
        name="shift-swap-request-approve",
    ),
    path(
        "businesses/<uuid:business_id>/shift-swap-requests/<uuid:pk>/reject/",
        swap_request_reject,
        name="shift-swap-request-reject",
    ),
    path("businesses/<uuid:business_id>/time-off-requests/", time_off_list, name="time-off-request-list"),
    path(
        "businesses/<uuid:business_id>/time-off-requests/<uuid:pk>/",
        time_off_detail,
        name="time-off-request-detail",
    ),
    path(
        "businesses/<uuid:business_id>/time-off-requests/<uuid:pk>/approve/",
        time_off_approve,
        name="time-off-request-approve",
    ),
    path(
        "businesses/<uuid:business_id>/time-off-requests/<uuid:pk>/reject/",
        time_off_reject,
        name="time-off-request-reject",
    ),
    path("businesses/<uuid:business_id>/pay-stubs/", pay_stub_list, name="pay-stub-list"),
    path("businesses/<uuid:business_id>/pay-stubs/<uuid:pk>/", pay_stub_detail, name="pay-stub-detail"),
    path(
        "businesses/<uuid:business_id>/pay-stubs/generate/",
        pay_stub_generate,
        name="pay-stub-generate",
    ),
]
