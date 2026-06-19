from django.urls import path

from .views import (
    FormSubmissionViewSet,
    GoogleAdsCampaignViewSet,
    LeadViewSet,
    PageViewViewSet,
    TrackingEventViewSet,
    TrackingScriptViewSet,
    WebsiteVisitorViewSet,
)

app_name = "marketing"

script_list = TrackingScriptViewSet.as_view({"get": "list", "post": "create"})
script_detail = TrackingScriptViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
script_regenerate = TrackingScriptViewSet.as_view({"post": "regenerate_key"})

visitor_list = WebsiteVisitorViewSet.as_view({"get": "list"})
visitor_detail = WebsiteVisitorViewSet.as_view({"get": "retrieve"})

page_view_list = PageViewViewSet.as_view({"get": "list"})
page_view_detail = PageViewViewSet.as_view({"get": "retrieve"})

tracking_event_list = TrackingEventViewSet.as_view({"get": "list"})
tracking_event_detail = TrackingEventViewSet.as_view({"get": "retrieve"})

lead_list = LeadViewSet.as_view({"get": "list", "post": "create"})
lead_detail = LeadViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

form_submission_list = FormSubmissionViewSet.as_view({"get": "list"})
form_submission_detail = FormSubmissionViewSet.as_view({"get": "retrieve"})

campaign_list = GoogleAdsCampaignViewSet.as_view({"get": "list", "post": "create"})
campaign_detail = GoogleAdsCampaignViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

urlpatterns = [
    path("businesses/<uuid:business_id>/tracking-scripts/", script_list, name="tracking-script-list"),
    path("businesses/<uuid:business_id>/tracking-scripts/<uuid:pk>/", script_detail, name="tracking-script-detail"),
    path(
        "businesses/<uuid:business_id>/tracking-scripts/<uuid:pk>/regenerate-key/",
        script_regenerate,
        name="tracking-script-regenerate-key",
    ),
    path("businesses/<uuid:business_id>/website-visitors/", visitor_list, name="website-visitor-list"),
    path("businesses/<uuid:business_id>/website-visitors/<uuid:pk>/", visitor_detail, name="website-visitor-detail"),
    path("businesses/<uuid:business_id>/page-views/", page_view_list, name="page-view-list"),
    path("businesses/<uuid:business_id>/page-views/<uuid:pk>/", page_view_detail, name="page-view-detail"),
    path("businesses/<uuid:business_id>/tracking-events/", tracking_event_list, name="tracking-event-list"),
    path(
        "businesses/<uuid:business_id>/tracking-events/<uuid:pk>/",
        tracking_event_detail,
        name="tracking-event-detail",
    ),
    path("businesses/<uuid:business_id>/leads/", lead_list, name="lead-list"),
    path("businesses/<uuid:business_id>/leads/<uuid:pk>/", lead_detail, name="lead-detail"),
    path("businesses/<uuid:business_id>/form-submissions/", form_submission_list, name="form-submission-list"),
    path(
        "businesses/<uuid:business_id>/form-submissions/<uuid:pk>/",
        form_submission_detail,
        name="form-submission-detail",
    ),
    path("businesses/<uuid:business_id>/google-ads-campaigns/", campaign_list, name="google-ads-campaign-list"),
    path(
        "businesses/<uuid:business_id>/google-ads-campaigns/<uuid:pk>/",
        campaign_detail,
        name="google-ads-campaign-detail",
    ),
]
