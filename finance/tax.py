"""
Direct port of the old frontend's tax-utils.ts. Same tax types, same
rates, same calculation order — the only deliberate change is computing
in Decimal rather than floating point (rounding precision, not the
algorithm or the rates), consistent with every other currency field in
this codebase.

Original source (tax-utils.ts), for reference:

    export type TaxType = "ZERO" | "GST_5" | "HST_15" | "GST_QST_14975";

    export const TAX_OPTIONS: { value: TaxType; label: string; rate: number }[] = [
      { value: "ZERO", label: "No Tax (0%)", rate: 0 },
      { value: "GST_5", label: "GST (5%)", rate: 0.05 },
      { value: "HST_15", label: "HST (15%)", rate: 0.15 },
      { value: "GST_QST_14975", label: "GST + QST (14.975%)", rate: 0.14975 },
    ];

    export function getTaxRate(taxType: TaxType): number {
      return TAX_OPTIONS.find((t) => t.value === taxType)?.rate || 0;
    }

    export function calculateTotals(items, discountType, discountValue, taxType) {
      const subtotal = items.reduce((sum, item) => sum + item.quantity * item.rate, 0);
      let discount = 0;
      if (discountType === "percentage") discount = subtotal * (discountValue / 100);
      else if (discountType === "fixed") discount = discountValue;
      const taxableAmount = Math.max(0, subtotal - discount);
      const tax = taxableAmount * getTaxRate(taxType);
      const total = taxableAmount + tax;
      return { subtotal, discount, taxableAmount, tax, total };
    }

There is no per-line-item tax rate and no province/region lookup in the
original — tax is a single selection (`taxType`) applied once to the
whole document's taxable amount after discount. `Invoice`/`Estimate`
carry `tax_type`/`discount_type`/`discount_value` accordingly (see
models.py); `InvoiceLineItem`/`EstimateLineItem` do not have a tax field,
because the source logic never reads one.

An unsupported/unrecognized tax type is not a silent zero — see
`get_tax_rate`'s docstring below for why `ZERO` and "unrecognized" are
different cases here, both behaving the same way as the original's
`|| 0`, which is a deliberate (if slightly surprising) fallback baked into
the source being ported, not something invented here.

`QST_9975` ("QST (9.975%)", rate 0.09975) was added after the initial
port, on explicit instruction with the exact value to use — it is not in
the original tax-utils.ts source quoted above, and not something guessed
at here. Same fallback/computation rules as every other `TaxType` value
apply to it; nothing else about `calculate_totals`'s logic changed to
accommodate it.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import List

from django.db import models

# Currency precision for every value calculate_totals returns.
CURRENCY_QUANTIZE = Decimal("0.01")


class TaxType(models.TextChoices):
    ZERO = "ZERO", "No Tax (0%)"
    GST_5 = "GST_5", "GST (5%)"
    HST_15 = "HST_15", "HST (15%)"
    GST_QST_14975 = "GST_QST_14975", "GST + QST (14.975%)"
    QST_9975 = "QST_9975", "QST (9.975%)"


class DiscountType(models.TextChoices):
    NONE = "none", "None"
    PERCENTAGE = "percentage", "Percentage"
    FIXED = "fixed", "Fixed amount"


# Mirrors TAX_OPTIONS' `rate` column exactly (plus QST_9975 — see module docstring).
TAX_RATES = {
    TaxType.ZERO: Decimal("0"),
    TaxType.GST_5: Decimal("0.05"),
    TaxType.HST_15: Decimal("0.15"),
    TaxType.GST_QST_14975: Decimal("0.14975"),
    TaxType.QST_9975: Decimal("0.09975"),
}


def get_tax_rate(tax_type: str) -> Decimal:
    """
    Mirrors `TAX_OPTIONS.find(...)?.rate || 0` exactly: a tax_type that
    isn't in the table returns 0, the same way `ZERO` itself does. This
    means a typo'd/unsupported tax_type silently computes no tax rather
    than raising — that's the original's actual behavior (`|| 0`), not an
    approximation introduced here. `Invoice`/`Estimate.tax_type` are a
    `TextChoices` field, so in practice the only way to hit this fallback
    is direct ORM/admin misuse bypassing serializer validation, not normal
    API usage (the serializer's ChoiceField already rejects anything
    outside TaxType.choices before this is ever called).
    """
    return TAX_RATES.get(tax_type, Decimal("0"))


@dataclass(frozen=True)
class TaxLineItem:
    quantity: Decimal
    rate: Decimal


@dataclass(frozen=True)
class TotalsResult:
    subtotal: Decimal
    discount: Decimal
    taxable_amount: Decimal
    tax: Decimal
    total: Decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CURRENCY_QUANTIZE, rounding=ROUND_HALF_UP)


def calculate_totals(
    items: List[TaxLineItem], discount_type: str, discount_value: Decimal, tax_type: str
) -> TotalsResult:
    """Direct port of calculateTotals() — see module docstring for the original."""
    subtotal = sum((item.quantity * item.rate for item in items), Decimal("0"))

    discount = Decimal("0")
    if discount_type == DiscountType.PERCENTAGE:
        discount = subtotal * (discount_value / Decimal("100"))
    elif discount_type == DiscountType.FIXED:
        discount = discount_value

    taxable_amount = max(Decimal("0"), subtotal - discount)
    tax = taxable_amount * get_tax_rate(tax_type)
    total = taxable_amount + tax

    return TotalsResult(
        subtotal=_quantize(subtotal),
        discount=_quantize(discount),
        taxable_amount=_quantize(taxable_amount),
        tax=_quantize(tax),
        total=_quantize(total),
    )
