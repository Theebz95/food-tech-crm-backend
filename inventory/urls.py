from django.urls import path

from .views import InventoryItemViewSet, InventoryTransactionViewSet, VendorViewSet

app_name = "inventory"

vendor_list = VendorViewSet.as_view({"get": "list", "post": "create"})
vendor_detail = VendorViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

item_list = InventoryItemViewSet.as_view({"get": "list", "post": "create"})
item_detail = InventoryItemViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
item_low_stock = InventoryItemViewSet.as_view({"get": "low_stock"})
item_adjust_stock = InventoryItemViewSet.as_view({"post": "adjust_stock"})

transaction_list = InventoryTransactionViewSet.as_view({"get": "list"})
transaction_detail = InventoryTransactionViewSet.as_view({"get": "retrieve"})

urlpatterns = [
    path("businesses/<uuid:business_id>/vendors/", vendor_list, name="vendor-list"),
    path("businesses/<uuid:business_id>/vendors/<uuid:pk>/", vendor_detail, name="vendor-detail"),
    path("businesses/<uuid:business_id>/inventory-items/", item_list, name="item-list"),
    path("businesses/<uuid:business_id>/inventory-items/low-stock/", item_low_stock, name="item-low-stock"),
    path("businesses/<uuid:business_id>/inventory-items/<uuid:pk>/", item_detail, name="item-detail"),
    path(
        "businesses/<uuid:business_id>/inventory-items/<uuid:pk>/adjust-stock/",
        item_adjust_stock,
        name="item-adjust-stock",
    ),
    path(
        "businesses/<uuid:business_id>/inventory-transactions/", transaction_list, name="transaction-list"
    ),
    path(
        "businesses/<uuid:business_id>/inventory-transactions/<uuid:pk>/",
        transaction_detail,
        name="transaction-detail",
    ),
]
