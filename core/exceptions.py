"""
Global DRF exception handler.

`django.db.models.ProtectedError` — raised whenever a PROTECT'd FK blocks
a delete (Order.customer, Invoice.customer, Bill.vendor, Estimate.customer,
CustomerLoyaltyAccount.customer/loyalty_program,
PointsTransaction.account, GiftCardTransaction.gift_card, Position,
ShiftTemplate, ...) — has no handling in DRF's default exception_handler,
so it was surfacing as a raw 500 instead of the clean 400 the PROTECT
choice was actually meant to produce: "no, you can't delete this, it has
real history." The PROTECT decision was already made at the model layer
for each of these; this just makes that decision visible as a normal API
error instead of an unhandled crash.
"""

from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    if isinstance(exc, ProtectedError):
        return Response(
            {"detail": "Cannot delete: other records still reference this. Deactivate it instead, or remove those records first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None
