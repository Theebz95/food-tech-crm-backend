"""
Pay stub calculation.

Fixes the Phase 1 audit finding that gross/net pay was computed client-side
(PayStubs.tsx) with no overtime or tax logic and no validation at all.
Hours come from real TimeEntry rows (clock_out_at - clock_in_at, minus any
closed breaks) — never manual entry.

*** TAX CALCULATION IS A PLACEHOLDER. ***
`PLACEHOLDER_FLAT_TAX_RATE` below is a single flat percentage applied to
gross pay. It has no concept of tax brackets, filing status, jurisdiction,
FICA/social security, benefits deductions, or any other real payroll tax
rule. This exists only so PayStub has a structurally complete net_pay for
development/demo purposes. Treat this as one step above hardcoding net_pay
to zero — review and replace with real, jurisdiction-correct tax logic
(ideally via a payroll tax provider/API, not hand-rolled math) and get
accountant/legal sign-off before this is anywhere near real payroll. See
README "Pay stub tax disclaimer".

Overtime: regular vs overtime hours are split per ISO week (Mon-Sun), not
per pay period, since "40 hrs/week" is a weekly threshold even across a
biweekly or monthly pay period. The threshold is a Business-level setting
(`Business.extra_settings["overtime_threshold_hours"]`, default 40) rather
than a new column, consistent with how Business.extra_settings is already
documented as the place for free-form per-business configuration.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from .models import PayStub, TimeEntry

OVERTIME_MULTIPLIER = Decimal("1.5")
DEFAULT_OVERTIME_THRESHOLD_HOURS = Decimal("40")
PLACEHOLDER_FLAT_TAX_RATE = Decimal("0.15")
TAX_DISCLAIMER = (
    "PLACEHOLDER flat-rate deduction only — not compliant payroll tax logic. "
    "Must be reviewed by an accountant/legal professional before use on real payroll."
)

CENTS = Decimal("0.01")


class PayStubAlreadyExistsError(Exception):
    pass


def _worked_hours_by_iso_week(membership, period_start, period_end):
    """
    {(iso_year, iso_week): Decimal hours} for every *completed* TimeEntry
    starting within the period, net of any closed breaks. Open entries
    (still clocked in) don't count yet — they have no end time to measure.
    """
    entries = TimeEntry.objects.filter(
        membership=membership,
        status=TimeEntry.Status.CLOCKED_OUT,
        clock_in_at__date__gte=period_start,
        clock_in_at__date__lte=period_end,
    ).prefetch_related("breaks")

    hours_by_week = defaultdict(Decimal)
    for entry in entries:
        worked = entry.clock_out_at - entry.clock_in_at
        break_total = sum(
            (b.break_end_at - b.break_start_at for b in entry.breaks.all() if b.break_end_at is not None),
            start=timedelta(),
        )
        net_seconds = (worked - break_total).total_seconds()
        iso_year, iso_week, _ = entry.clock_in_at.isocalendar()
        hours_by_week[(iso_year, iso_week)] += Decimal(net_seconds) / Decimal(3600)

    return hours_by_week


def generate_pay_stub(membership, position, period_start, period_end) -> PayStub:
    with transaction.atomic():
        if PayStub.objects.filter(
            membership=membership, pay_period_start=period_start, pay_period_end=period_end
        ).exists():
            raise PayStubAlreadyExistsError("A pay stub already exists for this membership and period.")

        threshold = Decimal(
            str(membership.business.extra_settings.get("overtime_threshold_hours", DEFAULT_OVERTIME_THRESHOLD_HOURS))
        )
        hours_by_week = _worked_hours_by_iso_week(membership, period_start, period_end)

        regular_total = Decimal("0.00")
        overtime_total = Decimal("0.00")
        weekly_breakdown = []
        for (iso_year, iso_week), worked in sorted(hours_by_week.items()):
            worked = worked.quantize(CENTS)
            regular = min(worked, threshold)
            overtime = max(Decimal("0.00"), worked - threshold)
            regular_total += regular
            overtime_total += overtime
            weekly_breakdown.append(
                {
                    "iso_year": iso_year,
                    "iso_week": iso_week,
                    "worked_hours": str(worked),
                    "regular_hours": str(regular),
                    "overtime_hours": str(overtime),
                }
            )

        regular_rate = position.hourly_rate
        overtime_rate = (regular_rate * OVERTIME_MULTIPLIER).quantize(CENTS)
        gross_pay = (regular_total * regular_rate + overtime_total * overtime_rate).quantize(CENTS)
        tax_amount = (gross_pay * PLACEHOLDER_FLAT_TAX_RATE).quantize(CENTS)
        net_pay = gross_pay - tax_amount

        breakdown = {
            "overtime_threshold_hours_per_week": str(threshold),
            "overtime_multiplier": str(OVERTIME_MULTIPLIER),
            "regular_rate": str(regular_rate),
            "overtime_rate": str(overtime_rate),
            "weekly_breakdown": weekly_breakdown,
            "gross_pay": str(gross_pay),
            "tax_rate": str(PLACEHOLDER_FLAT_TAX_RATE),
            "tax_amount": str(tax_amount),
            "net_pay": str(net_pay),
            "tax_disclaimer": TAX_DISCLAIMER,
        }

        return PayStub.objects.create(
            membership=membership,
            position=position,
            pay_period_start=period_start,
            pay_period_end=period_end,
            regular_hours=regular_total,
            overtime_hours=overtime_total,
            gross_pay=gross_pay,
            net_pay=net_pay,
            breakdown=breakdown,
        )
