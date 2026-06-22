from django.urls import path

from .views import (
    CustomerLoyaltyAccountViewSet,
    GiftCardTransactionViewSet,
    GiftCardViewSet,
    LoyaltyProgramViewSet,
    OrderViewSet,
    PointsTransactionViewSet,
)

app_name = "loyalty"

program_list = LoyaltyProgramViewSet.as_view({"get": "list", "post": "create"})
program_detail = LoyaltyProgramViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

account_list = CustomerLoyaltyAccountViewSet.as_view({"get": "list", "post": "create"})
account_detail = CustomerLoyaltyAccountViewSet.as_view({"get": "retrieve", "delete": "destroy"})
account_award_points = CustomerLoyaltyAccountViewSet.as_view({"post": "award_points"})
account_redeem_points = CustomerLoyaltyAccountViewSet.as_view({"post": "redeem_points"})

points_transaction_list = PointsTransactionViewSet.as_view({"get": "list"})
points_transaction_detail = PointsTransactionViewSet.as_view({"get": "retrieve"})

order_list = OrderViewSet.as_view({"get": "list", "post": "create"})
order_detail = OrderViewSet.as_view({"get": "retrieve"})
order_cancel = OrderViewSet.as_view({"post": "cancel"})
order_convert_to_invoice = OrderViewSet.as_view({"post": "convert_to_invoice"})

gift_card_list = GiftCardViewSet.as_view({"get": "list", "post": "create"})
gift_card_detail = GiftCardViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
gift_card_reload = GiftCardViewSet.as_view({"post": "reload"})
gift_card_redeem = GiftCardViewSet.as_view({"post": "redeem"})
gift_card_send = GiftCardViewSet.as_view({"post": "send"})
gift_card_qr_code = GiftCardViewSet.as_view({"get": "qr_code"})

gift_card_transaction_list = GiftCardTransactionViewSet.as_view({"get": "list"})
gift_card_transaction_detail = GiftCardTransactionViewSet.as_view({"get": "retrieve"})

urlpatterns = [
    path("businesses/<uuid:business_id>/loyalty-programs/", program_list, name="loyalty-program-list"),
    path("businesses/<uuid:business_id>/loyalty-programs/<uuid:pk>/", program_detail, name="loyalty-program-detail"),
    path("businesses/<uuid:business_id>/loyalty-accounts/", account_list, name="loyalty-account-list"),
    path("businesses/<uuid:business_id>/loyalty-accounts/<uuid:pk>/", account_detail, name="loyalty-account-detail"),
    path(
        "businesses/<uuid:business_id>/loyalty-accounts/<uuid:pk>/award-points/",
        account_award_points,
        name="loyalty-account-award-points",
    ),
    path(
        "businesses/<uuid:business_id>/loyalty-accounts/<uuid:pk>/redeem-points/",
        account_redeem_points,
        name="loyalty-account-redeem-points",
    ),
    path("businesses/<uuid:business_id>/points-transactions/", points_transaction_list, name="points-transaction-list"),
    path(
        "businesses/<uuid:business_id>/points-transactions/<uuid:pk>/",
        points_transaction_detail,
        name="points-transaction-detail",
    ),
    path("businesses/<uuid:business_id>/orders/", order_list, name="order-list"),
    path("businesses/<uuid:business_id>/orders/<uuid:pk>/", order_detail, name="order-detail"),
    path("businesses/<uuid:business_id>/orders/<uuid:pk>/cancel/", order_cancel, name="order-cancel"),
    path(
        "businesses/<uuid:business_id>/orders/<uuid:pk>/convert-to-invoice/",
        order_convert_to_invoice,
        name="order-convert-to-invoice",
    ),
    path("businesses/<uuid:business_id>/gift-cards/", gift_card_list, name="gift-card-list"),
    path("businesses/<uuid:business_id>/gift-cards/<uuid:pk>/", gift_card_detail, name="gift-card-detail"),
    path("businesses/<uuid:business_id>/gift-cards/<uuid:pk>/reload/", gift_card_reload, name="gift-card-reload"),
    path("businesses/<uuid:business_id>/gift-cards/<uuid:pk>/redeem/", gift_card_redeem, name="gift-card-redeem"),
    path("businesses/<uuid:business_id>/gift-cards/<uuid:pk>/send/", gift_card_send, name="gift-card-send"),
    path(
        "businesses/<uuid:business_id>/gift-cards/<uuid:pk>/qr-code/", gift_card_qr_code, name="gift-card-qr-code"
    ),
    path(
        "businesses/<uuid:business_id>/gift-card-transactions/",
        gift_card_transaction_list,
        name="gift-card-transaction-list",
    ),
    path(
        "businesses/<uuid:business_id>/gift-card-transactions/<uuid:pk>/",
        gift_card_transaction_detail,
        name="gift-card-transaction-detail",
    ),
]
