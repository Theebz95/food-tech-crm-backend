"""
AR/AP aging reports — server-side bucket aggregation.

The actual fix (Phase 1 audit finding): the old reports loaded every
invoice/bill client-side and bucketed them in the browser, with no
pagination. The bucket *summary* returned here is computed once,
server-side, from the real unpaid balance (total - paid_total) — never a
client-sent number — and is small/fixed-size by construction (5 buckets),
so it doesn't need pagination itself. The underlying rows that make up
one bucket can be large, though, so views.py paginates that detail list
(`?bucket=...`) rather than returning it as one dump — see
ARAgingReportView/APAgingReportView.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List

from django.utils import timezone

from .models import Bill, Invoice

AGING_BUCKETS = ["current", "1_30", "31_60", "61_90", "90_plus"]


@dataclass
class AgingReport:
    as_of: object
    bucket_totals: Dict[str, Decimal]
    bucket_counts: Dict[str, int]
    bucket_rows: Dict[str, List[object]] = field(default_factory=dict)

    @property
    def grand_total(self) -> Decimal:
        return sum(self.bucket_totals.values(), Decimal("0"))


def _bucket_for_days_overdue(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1_30"
    if days_overdue <= 60:
        return "31_60"
    if days_overdue <= 90:
        return "61_90"
    return "90_plus"


def _aging_report(queryset, active_statuses, as_of=None) -> AgingReport:
    as_of = as_of or timezone.now().date()
    bucket_totals = {key: Decimal("0") for key in AGING_BUCKETS}
    bucket_counts = {key: 0 for key in AGING_BUCKETS}
    bucket_rows = {key: [] for key in AGING_BUCKETS}

    for row in queryset.filter(status__in=active_statuses).exclude(due_date__isnull=True):
        balance = row.total - row.paid_total
        if balance <= 0:
            continue
        days_overdue = (as_of - row.due_date).days
        bucket = _bucket_for_days_overdue(days_overdue)
        bucket_totals[bucket] += balance
        bucket_counts[bucket] += 1
        bucket_rows[bucket].append(row)

    return AgingReport(as_of=as_of, bucket_totals=bucket_totals, bucket_counts=bucket_counts, bucket_rows=bucket_rows)


def ar_aging_report(business, as_of=None) -> AgingReport:
    """Unpaid/overdue Invoices — money owed *to* the business."""
    queryset = Invoice.objects.filter(business=business)
    return _aging_report(queryset, (Invoice.Status.SENT, Invoice.Status.OVERDUE), as_of)


def ap_aging_report(business, as_of=None) -> AgingReport:
    """Unpaid/overdue Bills — money the business owes."""
    queryset = Bill.objects.filter(business=business)
    return _aging_report(queryset, (Bill.Status.RECEIVED, Bill.Status.OVERDUE), as_of)
