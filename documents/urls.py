from django.urls import path

from .views import DocumentViewSet

app_name = "documents"

document_list = DocumentViewSet.as_view({"get": "list", "post": "create"})
document_detail = DocumentViewSet.as_view({"get": "retrieve", "delete": "destroy"})
document_download = DocumentViewSet.as_view({"get": "download"})

urlpatterns = [
    path("businesses/<uuid:business_id>/documents/", document_list, name="document-list"),
    path("businesses/<uuid:business_id>/documents/<uuid:pk>/", document_detail, name="document-detail"),
    path(
        "businesses/<uuid:business_id>/documents/<uuid:pk>/download/",
        document_download,
        name="document-download",
    ),
]
