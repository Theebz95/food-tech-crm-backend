"""
Finance staff-side endpoints (chart of accounts, invoices, payments,
estimates, invoice templates, bills, bill payments, bank transactions,
recurring transactions, AR/AP aging reports), tenant-scoped via
core.permissions.HasBusinessRole. See webhooks.py for the separate,
unauthenticated Stripe webhook.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import reports, services
from .models import (
    BankTransaction,
    Bill,
    BillPayment,
    ChartOfAccount,
    Estimate,
    Invoice,
    InvoiceTemplate,
    Payment,
    RecurringTransaction,
)
from .reports import AGING_BUCKETS
from .serializers import (
    BankTransactionSerializer,
    BillPaymentSerializer,
    BillSerializer,
    BillWriteSerializer,
    ChartOfAccountSerializer,
    EstimateSerializer,
    EstimateWriteSerializer,
    InvoiceSerializer,
    InvoiceTemplateSerializer,
    InvoiceWriteSerializer,
    PaymentSerializer,
    ReconcileBankTransactionSerializer,
    RecordBillPaymentSerializer,
    RecordPaymentSerializer,
    RecurringTransactionSerializer,
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


class ChartOfAccountViewSet(_BusinessScopedViewSet):
    serializer_class = ChartOfAccountSerializer

    def get_queryset(self):
        return ChartOfAccount.objects.filter(business_id=self.kwargs["business_id"])


class BankTransactionViewSet(_BusinessScopedViewSet):
    serializer_class = BankTransactionSerializer

    def get_queryset(self):
        return BankTransaction.objects.filter(business_id=self.kwargs["business_id"])

    @action(detail=True, methods=["post"])
    def reconcile(self, request, business_id=None, pk=None):
        bank_transaction = self.get_object()
        input_serializer = ReconcileBankTransactionSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        try:
            bank_transaction = services.reconcile_bank_transaction(
                bank_transaction, input_serializer.validated_data["target_object"]
            )
        except services.FinanceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BankTransactionSerializer(bank_transaction).data)


class RecurringTransactionViewSet(_BusinessScopedViewSet):
    serializer_class = RecurringTransactionSerializer

    def get_queryset(self):
        return RecurringTransaction.objects.filter(business_id=self.kwargs["business_id"])


class InvoiceTemplateViewSet(_BusinessScopedViewSet):
    serializer_class = InvoiceTemplateSerializer

    def get_queryset(self):
        return InvoiceTemplate.objects.filter(business_id=self.kwargs["business_id"])


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only — the only way a Payment gets created is InvoiceViewSet.record_payment."""

    serializer_class = PaymentSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return Payment.objects.filter(business_id=self.kwargs["business_id"])


class BillPaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only — the only way a BillPayment gets created is BillViewSet.record_payment."""

    serializer_class = BillPaymentSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_queryset(self):
        return BillPayment.objects.filter(business_id=self.kwargs["business_id"])


class InvoiceViewSet(viewsets.ModelViewSet):
    """
    create/update go through InvoiceWriteSerializer + finance/services.py
    (never a generic ModelSerializer.save()) since totals/invoice_number
    are server-computed. status only changes via send/cancel/record-payment.
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
        return Invoice.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def list(self, request, business_id=None):
        page = self.get_queryset()
        return Response(InvoiceSerializer(page, many=True).data)

    def retrieve(self, request, business_id=None, pk=None):
        return Response(InvoiceSerializer(self.get_object()).data)

    def create(self, request, business_id=None):
        serializer = InvoiceWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        invoice = services.create_invoice(
            business=self.get_business(),
            customer=data["customer"],
            line_items_data=data["line_items"],
            tax_type=data["tax_type"],
            discount_type=data["discount_type"],
            discount_value=data["discount_value"],
            due_date=data.get("due_date"),
            notes=data.get("notes", ""),
            revenue_account=data.get("revenue_account"),
        )
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    def update(self, request, business_id=None, pk=None, partial=False):
        invoice = self.get_object()
        serializer = InvoiceWriteSerializer(
            data=request.data, context=self.get_serializer_context(), partial=True
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        line_items_data = data.pop("line_items", None)
        try:
            invoice = services.update_invoice(invoice, line_items_data=line_items_data, **data)
        except services.InvalidInvoiceStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InvoiceSerializer(invoice).data)

    def partial_update(self, request, business_id=None, pk=None):
        return self.update(request, business_id=business_id, pk=pk, partial=True)

    @action(detail=True, methods=["post"])
    def send(self, request, business_id=None, pk=None):
        invoice = self.get_object()
        try:
            invoice = services.send_invoice(invoice)
        except services.InvalidInvoiceStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, business_id=None, pk=None):
        invoice = self.get_object()
        try:
            invoice = services.cancel_invoice(invoice)
        except services.InvalidInvoiceStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, business_id=None, pk=None):
        invoice = self.get_object()
        input_serializer = RecordPaymentSerializer(data=request.data, context=self.get_serializer_context())
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            payment = services.record_payment(
                invoice,
                amount=data["amount"],
                method=data["method"],
                membership=getattr(request, "business_membership", None),
                stripe_payment_intent_id=data.get("stripe_payment_intent_id", ""),
                deposit_account=data.get("deposit_account"),
                notes=data.get("notes", ""),
            )
        except services.FinanceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        invoice.refresh_from_db()
        return Response(
            {"invoice": InvoiceSerializer(invoice).data, "payment": PaymentSerializer(payment).data},
            status=status.HTTP_201_CREATED,
        )


class EstimateViewSet(viewsets.ModelViewSet):
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
        return Estimate.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def list(self, request, business_id=None):
        return Response(EstimateSerializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, business_id=None, pk=None):
        return Response(EstimateSerializer(self.get_object()).data)

    def create(self, request, business_id=None):
        serializer = EstimateWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        estimate = services.create_estimate(
            business=self.get_business(),
            customer=data["customer"],
            line_items_data=data["line_items"],
            tax_type=data["tax_type"],
            discount_type=data["discount_type"],
            discount_value=data["discount_value"],
            expires_at=data.get("expires_at"),
            notes=data.get("notes", ""),
        )
        return Response(EstimateSerializer(estimate).data, status=status.HTTP_201_CREATED)

    def update(self, request, business_id=None, pk=None, partial=False):
        estimate = self.get_object()
        serializer = EstimateWriteSerializer(
            data=request.data, context=self.get_serializer_context(), partial=True
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        line_items_data = data.pop("line_items", None)
        try:
            estimate = services.update_estimate(estimate, line_items_data=line_items_data, **data)
        except services.InvalidEstimateStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(EstimateSerializer(estimate).data)

    def partial_update(self, request, business_id=None, pk=None):
        return self.update(request, business_id=business_id, pk=pk, partial=True)

    @action(detail=True, methods=["post"], url_path="convert-to-invoice")
    def convert_to_invoice(self, request, business_id=None, pk=None):
        estimate = self.get_object()
        due_date = request.data.get("due_date")
        try:
            invoice = services.convert_estimate_to_invoice(estimate, due_date=due_date)
        except services.InvalidEstimateStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


class BillViewSet(viewsets.ModelViewSet):
    """Mirrors InvoiceViewSet exactly — see its docstring; same reasoning, owed to a Vendor instead of by a Customer."""

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
        return Bill.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def list(self, request, business_id=None):
        return Response(BillSerializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, business_id=None, pk=None):
        return Response(BillSerializer(self.get_object()).data)

    def create(self, request, business_id=None):
        serializer = BillWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        bill = services.create_bill(
            business=self.get_business(),
            vendor=data["vendor"],
            line_items_data=data["line_items"],
            tax_type=data["tax_type"],
            discount_type=data["discount_type"],
            discount_value=data["discount_value"],
            due_date=data.get("due_date"),
            notes=data.get("notes", ""),
            expense_account=data.get("expense_account"),
        )
        return Response(BillSerializer(bill).data, status=status.HTTP_201_CREATED)

    def update(self, request, business_id=None, pk=None, partial=False):
        bill = self.get_object()
        serializer = BillWriteSerializer(data=request.data, context=self.get_serializer_context(), partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        line_items_data = data.pop("line_items", None)
        try:
            bill = services.update_bill(bill, line_items_data=line_items_data, **data)
        except services.InvalidBillStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BillSerializer(bill).data)

    def partial_update(self, request, business_id=None, pk=None):
        return self.update(request, business_id=business_id, pk=pk, partial=True)

    @action(detail=True, methods=["post"])
    def receive(self, request, business_id=None, pk=None):
        bill = self.get_object()
        try:
            bill = services.receive_bill(bill)
        except services.InvalidBillStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BillSerializer(bill).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, business_id=None, pk=None):
        bill = self.get_object()
        try:
            bill = services.cancel_bill(bill)
        except services.InvalidBillStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BillSerializer(bill).data)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, business_id=None, pk=None):
        bill = self.get_object()
        input_serializer = RecordBillPaymentSerializer(data=request.data, context=self.get_serializer_context())
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            bill_payment = services.record_bill_payment(
                bill,
                amount=data["amount"],
                method=data["method"],
                membership=getattr(request, "business_membership", None),
                payment_account=data.get("payment_account"),
                notes=data.get("notes", ""),
            )
        except services.FinanceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        bill.refresh_from_db()
        return Response(
            {"bill": BillSerializer(bill).data, "bill_payment": BillPaymentSerializer(bill_payment).data},
            status=status.HTTP_201_CREATED,
        )


class AgingReportDetailPagination(PageNumberPagination):
    page_size = 25


class _BaseAgingReportView(APIView):
    """
    GET with no params -> bucket summary (small/fixed-size, never
    paginated — see reports.py module docstring). GET ?bucket=<key> ->
    the paginated list of the actual rows in that one bucket — this is
    the "detail view" that must be paginated, not a dump of all rows.
    """

    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"
    pagination_class = AgingReportDetailPagination
    detail_serializer_class = None  # set by subclass

    def get_business(self):
        business = Business.objects.filter(
            id=self.kwargs["business_id"], id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_report(self, business):
        raise NotImplementedError

    def get(self, request, business_id=None):
        business = self.get_business()
        report = self.get_report(business)

        bucket_param = request.query_params.get("bucket")
        if bucket_param:
            if bucket_param not in AGING_BUCKETS:
                return Response({"detail": "Invalid bucket."}, status=status.HTTP_400_BAD_REQUEST)
            paginator = self.pagination_class()
            page = paginator.paginate_queryset(report.bucket_rows[bucket_param], request, view=self)
            serializer = self.detail_serializer_class(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        return Response(
            {
                "as_of": report.as_of.isoformat(),
                "buckets": {
                    key: {"count": report.bucket_counts[key], "total": str(report.bucket_totals[key])}
                    for key in AGING_BUCKETS
                },
                "grand_total": str(report.grand_total),
            }
        )


class ARAgingReportView(_BaseAgingReportView):
    detail_serializer_class = InvoiceSerializer

    def get_report(self, business):
        return reports.ar_aging_report(business)


class APAgingReportView(_BaseAgingReportView):
    detail_serializer_class = BillSerializer

    def get_report(self, business):
        return reports.ap_aging_report(business)
