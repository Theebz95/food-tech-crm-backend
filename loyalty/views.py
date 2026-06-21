"""
Loyalty/order/gift-card staff-side endpoints, tenant-scoped via
core.permissions.HasBusinessRole. Balance/tier-changing actions
(award-points, redeem-points, reload, redeem) are dedicated, explicit
endpoints calling loyalty/services.py — never a generic field PATCH.
"""

from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.email import EmailSendError
from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import services
from .models import CustomerLoyaltyAccount, GiftCard, GiftCardTransaction, LoyaltyProgram, Order, PointsTransaction
from .qr import generate_qr_png
from .serializers import (
    AwardPointsSerializer,
    CustomerLoyaltyAccountSerializer,
    GiftCardCreateSerializer,
    GiftCardSerializer,
    GiftCardTransactionSerializer,
    LoyaltyProgramSerializer,
    OrderSerializer,
    OrderWriteSerializer,
    PointsTransactionSerializer,
    RedeemGiftCardSerializer,
    RedeemPointsSerializer,
    ReloadGiftCardSerializer,
)


class _BusinessScopedViewSet(viewsets.ModelViewSet):
    """For models with a direct `business` FK."""

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


class LoyaltyProgramViewSet(_BusinessScopedViewSet):
    serializer_class = LoyaltyProgramSerializer

    def get_queryset(self):
        return LoyaltyProgram.objects.filter(business_id=self.kwargs["business_id"])


class CustomerLoyaltyAccountViewSet(viewsets.ModelViewSet):
    """
    No direct `business` FK (only via customer/loyalty_program) — filtered
    accordingly, and perform_create is the plain ModelViewSet default
    (customer/loyalty_program are required input fields, already
    validated to belong to this business by the serializer).
    """

    serializer_class = CustomerLoyaltyAccountSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return CustomerLoyaltyAccount.objects.filter(loyalty_program__business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    @action(detail=True, methods=["post"], url_path="award-points")
    def award_points(self, request, business_id=None, pk=None):
        account = self.get_object()
        input_serializer = AwardPointsSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        txn = services.award_points(
            account, data["amount"], PointsTransaction.Reason.MANUAL, notes=data.get("notes", "")
        )
        account.refresh_from_db()
        return Response(
            {"account": CustomerLoyaltyAccountSerializer(account).data, "transaction": PointsTransactionSerializer(txn).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="redeem-points")
    def redeem_points(self, request, business_id=None, pk=None):
        account = self.get_object()
        input_serializer = RedeemPointsSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            txn = services.redeem_points(account, data["amount"], notes=data.get("notes", ""))
        except services.LoyaltyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        account.refresh_from_db()
        return Response(
            {"account": CustomerLoyaltyAccountSerializer(account).data, "transaction": PointsTransactionSerializer(txn).data},
            status=status.HTTP_201_CREATED,
        )


class PointsTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only — the only way one is created is the award/redeem actions above (or order creation/cancellation/expiration)."""

    serializer_class = PointsTransactionSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return PointsTransaction.objects.filter(account__loyalty_program__business_id=self.kwargs["business_id"])


class OrderViewSet(viewsets.ModelViewSet):
    """
    create goes through OrderWriteSerializer + services.create_order_and_award_points
    (never a generic ModelSerializer.save()). No generic update/destroy —
    urls.py only maps list/retrieve/create/cancel; PUT/PATCH/DELETE are
    simply never routed, so they 405 without needing extra code here.
    """

    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return Order.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def list(self, request, business_id=None):
        return Response(OrderSerializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, business_id=None, pk=None):
        return Response(OrderSerializer(self.get_object()).data)

    def create(self, request, business_id=None):
        serializer = OrderWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order = services.create_order_and_award_points(
            business=self.get_business(),
            customer=data["customer"],
            line_items_data=data["line_items"],
            tax_type=data["tax_type"],
            discount_type=data["discount_type"],
            discount_value=data["discount_value"],
            notes=data.get("notes", ""),
            loyalty_program=data.get("loyalty_program"),
        )
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, business_id=None, pk=None):
        order = self.get_object()
        try:
            order = services.cancel_order(order)
        except services.InvalidOrderStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(OrderSerializer(order).data)


class GiftCardViewSet(_BusinessScopedViewSet):
    """create goes through GiftCardCreateSerializer + services.create_gift_card."""

    serializer_class = GiftCardSerializer

    def get_queryset(self):
        return GiftCard.objects.filter(business_id=self.kwargs["business_id"])

    def create(self, request, business_id=None):
        serializer = GiftCardCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        card = services.create_gift_card(
            business=self.get_business(),
            initial_balance=data["initial_balance"],
            recipient_name=data.get("recipient_name", ""),
            recipient_email=data.get("recipient_email", ""),
            expires_at=data.get("expires_at"),
            purchaser_customer=data.get("purchaser_customer"),
        )
        return Response(GiftCardSerializer(card).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reload(self, request, business_id=None, pk=None):
        card = self.get_object()
        input_serializer = ReloadGiftCardSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        txn = services.reload_gift_card(
            card, data["amount"], membership=getattr(request, "business_membership", None), notes=data.get("notes", "")
        )
        card.refresh_from_db()
        return Response(
            {"gift_card": GiftCardSerializer(card).data, "transaction": GiftCardTransactionSerializer(txn).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def redeem(self, request, business_id=None, pk=None):
        card = self.get_object()
        input_serializer = RedeemGiftCardSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            txn = services.redeem_gift_card(
                card,
                data["amount"],
                membership=getattr(request, "business_membership", None),
                notes=data.get("notes", ""),
            )
        except services.LoyaltyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        card.refresh_from_db()
        return Response(
            {"gift_card": GiftCardSerializer(card).data, "transaction": GiftCardTransactionSerializer(txn).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def send(self, request, business_id=None, pk=None):
        card = self.get_object()
        try:
            card = services.send_gift_card_email(card)
        except services.LoyaltyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EmailSendError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(GiftCardSerializer(card).data)

    @action(detail=True, methods=["get"], url_path="qr-code")
    def qr_code(self, request, business_id=None, pk=None):
        card = self.get_object()
        return HttpResponse(generate_qr_png(card.code), content_type="image/png")


class GiftCardTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only — the only way one is created is GiftCardViewSet.reload/redeem (or creation itself)."""

    serializer_class = GiftCardTransactionSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return GiftCardTransaction.objects.filter(gift_card__business_id=self.kwargs["business_id"])
