from django.urls import path

from .views import (
    APAgingReportView,
    ARAgingReportView,
    BankTransactionViewSet,
    BillPaymentViewSet,
    BillViewSet,
    ChartOfAccountViewSet,
    EstimateViewSet,
    InvoiceTemplateViewSet,
    InvoiceViewSet,
    PaymentViewSet,
    RecurringTransactionViewSet,
)

app_name = "finance"

account_list = ChartOfAccountViewSet.as_view({"get": "list", "post": "create"})
account_detail = ChartOfAccountViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

invoice_list = InvoiceViewSet.as_view({"get": "list", "post": "create"})
invoice_detail = InvoiceViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
invoice_send = InvoiceViewSet.as_view({"post": "send"})
invoice_cancel = InvoiceViewSet.as_view({"post": "cancel"})
invoice_record_payment = InvoiceViewSet.as_view({"post": "record_payment"})

payment_list = PaymentViewSet.as_view({"get": "list"})
payment_detail = PaymentViewSet.as_view({"get": "retrieve"})

estimate_list = EstimateViewSet.as_view({"get": "list", "post": "create"})
estimate_detail = EstimateViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
estimate_convert = EstimateViewSet.as_view({"post": "convert_to_invoice"})

template_list = InvoiceTemplateViewSet.as_view({"get": "list", "post": "create"})
template_detail = InvoiceTemplateViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

bill_list = BillViewSet.as_view({"get": "list", "post": "create"})
bill_detail = BillViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
bill_receive = BillViewSet.as_view({"post": "receive"})
bill_cancel = BillViewSet.as_view({"post": "cancel"})
bill_record_payment = BillViewSet.as_view({"post": "record_payment"})

bill_payment_list = BillPaymentViewSet.as_view({"get": "list"})
bill_payment_detail = BillPaymentViewSet.as_view({"get": "retrieve"})

bank_transaction_list = BankTransactionViewSet.as_view({"get": "list", "post": "create"})
bank_transaction_detail = BankTransactionViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)
bank_transaction_reconcile = BankTransactionViewSet.as_view({"post": "reconcile"})

recurring_transaction_list = RecurringTransactionViewSet.as_view({"get": "list", "post": "create"})
recurring_transaction_detail = RecurringTransactionViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

urlpatterns = [
    path("businesses/<uuid:business_id>/accounts/", account_list, name="account-list"),
    path("businesses/<uuid:business_id>/accounts/<uuid:pk>/", account_detail, name="account-detail"),
    path("businesses/<uuid:business_id>/invoices/", invoice_list, name="invoice-list"),
    path("businesses/<uuid:business_id>/invoices/<uuid:pk>/", invoice_detail, name="invoice-detail"),
    path("businesses/<uuid:business_id>/invoices/<uuid:pk>/send/", invoice_send, name="invoice-send"),
    path("businesses/<uuid:business_id>/invoices/<uuid:pk>/cancel/", invoice_cancel, name="invoice-cancel"),
    path(
        "businesses/<uuid:business_id>/invoices/<uuid:pk>/record-payment/",
        invoice_record_payment,
        name="invoice-record-payment",
    ),
    path("businesses/<uuid:business_id>/payments/", payment_list, name="payment-list"),
    path("businesses/<uuid:business_id>/payments/<uuid:pk>/", payment_detail, name="payment-detail"),
    path("businesses/<uuid:business_id>/estimates/", estimate_list, name="estimate-list"),
    path("businesses/<uuid:business_id>/estimates/<uuid:pk>/", estimate_detail, name="estimate-detail"),
    path(
        "businesses/<uuid:business_id>/estimates/<uuid:pk>/convert-to-invoice/",
        estimate_convert,
        name="estimate-convert-to-invoice",
    ),
    path("businesses/<uuid:business_id>/invoice-templates/", template_list, name="invoice-template-list"),
    path(
        "businesses/<uuid:business_id>/invoice-templates/<uuid:pk>/",
        template_detail,
        name="invoice-template-detail",
    ),
    path("businesses/<uuid:business_id>/bills/", bill_list, name="bill-list"),
    path("businesses/<uuid:business_id>/bills/<uuid:pk>/", bill_detail, name="bill-detail"),
    path("businesses/<uuid:business_id>/bills/<uuid:pk>/receive/", bill_receive, name="bill-receive"),
    path("businesses/<uuid:business_id>/bills/<uuid:pk>/cancel/", bill_cancel, name="bill-cancel"),
    path(
        "businesses/<uuid:business_id>/bills/<uuid:pk>/record-payment/",
        bill_record_payment,
        name="bill-record-payment",
    ),
    path("businesses/<uuid:business_id>/bill-payments/", bill_payment_list, name="bill-payment-list"),
    path("businesses/<uuid:business_id>/bill-payments/<uuid:pk>/", bill_payment_detail, name="bill-payment-detail"),
    path("businesses/<uuid:business_id>/bank-transactions/", bank_transaction_list, name="bank-transaction-list"),
    path(
        "businesses/<uuid:business_id>/bank-transactions/<uuid:pk>/",
        bank_transaction_detail,
        name="bank-transaction-detail",
    ),
    path(
        "businesses/<uuid:business_id>/bank-transactions/<uuid:pk>/reconcile/",
        bank_transaction_reconcile,
        name="bank-transaction-reconcile",
    ),
    path(
        "businesses/<uuid:business_id>/recurring-transactions/",
        recurring_transaction_list,
        name="recurring-transaction-list",
    ),
    path(
        "businesses/<uuid:business_id>/recurring-transactions/<uuid:pk>/",
        recurring_transaction_detail,
        name="recurring-transaction-detail",
    ),
    path("businesses/<uuid:business_id>/reports/ar-aging/", ARAgingReportView.as_view(), name="ar-aging-report"),
    path("businesses/<uuid:business_id>/reports/ap-aging/", APAgingReportView.as_view(), name="ap-aging-report"),
]
