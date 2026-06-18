from django.urls import path

from .views import CustomerViewSet

app_name = "customers"

customer_list = CustomerViewSet.as_view({"get": "list", "post": "create"})
customer_detail = CustomerViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

urlpatterns = [
    path("businesses/<uuid:business_id>/customers/", customer_list, name="customer-list"),
    path("businesses/<uuid:business_id>/customers/<uuid:pk>/", customer_detail, name="customer-detail"),
]
