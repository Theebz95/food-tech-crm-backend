"""
Inventory & Vendors endpoints, tenant-scoped via core.permissions.HasBusinessRole.

InventoryTransaction has no create/update/delete endpoint at all — the
ledger is read-only via the API; the only way a row gets created is the
adjust-stock action below, which goes through inventory/services.py.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import services
from .models import InventoryItem, InventoryTransaction, Vendor
from .serializers import (
    AdjustStockSerializer,
    InventoryItemSerializer,
    InventoryTransactionSerializer,
    VendorSerializer,
)


class _BusinessScopedViewSet(viewsets.ModelViewSet):
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def perform_create(self, serializer):
        serializer.save(business=self.get_business())


class VendorViewSet(_BusinessScopedViewSet):
    serializer_class = VendorSerializer

    def get_queryset(self):
        return Vendor.objects.filter(business_id=self.kwargs["business_id"])


class InventoryItemViewSet(_BusinessScopedViewSet):
    serializer_class = InventoryItemSerializer

    def get_queryset(self):
        return InventoryItem.objects.filter(business_id=self.kwargs["business_id"])

    @action(detail=False, methods=["get"], url_path="low-stock")
    def low_stock(self, request, business_id=None):
        items = [item for item in self.get_queryset() if item.current_quantity <= item.low_stock_threshold]
        return Response(InventoryItemSerializer(items, many=True).data)

    @action(detail=True, methods=["post"], url_path="adjust-stock")
    def adjust_stock(self, request, business_id=None, pk=None):
        item = self.get_object()
        input_serializer = AdjustStockSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            entry = services.adjust_stock(
                item,
                data["delta"],
                data["transaction_type"],
                request.business_membership,
                reason=data.get("reason", ""),
            )
        except services.InsufficientStockError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        return Response(
            {
                "item": InventoryItemSerializer(item).data,
                "transaction": InventoryTransactionSerializer(entry).data,
            },
            status=status.HTTP_201_CREATED,
        )


class InventoryTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InventoryTransactionSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return InventoryTransaction.objects.filter(item__business_id=self.kwargs["business_id"])
