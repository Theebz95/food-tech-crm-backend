"""
Customer CRUD, tenant-scoped via core.permissions.HasBusinessRole.

Routed nested under a business (see urls.py:
businesses/<business_id>/customers/...) so HasBusinessRole's
`business_lookup_url_kwarg` check runs on every action, including
list/create — not just detail views.
"""

from rest_framework import viewsets
from rest_framework.exceptions import NotFound

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from .models import Customer
from .serializers import CustomerSerializer


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"

    def get_business(self):
        business_id = self.kwargs["business_id"]
        business = Business.objects.filter(
            id=business_id, id__in=business_ids_for_user(self.request.user)
        ).first()
        if business is None:
            raise NotFound("Business not found.")
        return business

    def get_queryset(self):
        return Customer.objects.filter(business_id=self.kwargs["business_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "business_id" in self.kwargs:
            context["business"] = self.get_business()
        return context

    def perform_create(self, serializer):
        serializer.save(business=self.get_business())
