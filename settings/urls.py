from django.urls import path

from .views import BusinessProfileLogoRemoveView, BusinessProfileLogoUploadView, BusinessProfileView

app_name = "business_settings"

urlpatterns = [
    path("businesses/<uuid:business_id>/profile/", BusinessProfileView.as_view(), name="business-profile"),
    path(
        "businesses/<uuid:business_id>/profile/upload-logo/",
        BusinessProfileLogoUploadView.as_view(),
        name="business-profile-upload-logo",
    ),
    path(
        "businesses/<uuid:business_id>/profile/logo/",
        BusinessProfileLogoRemoveView.as_view(),
        name="business-profile-remove-logo",
    ),
]
