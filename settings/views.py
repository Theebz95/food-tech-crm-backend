"""
Business settings endpoints, tenant-scoped via core.permissions.HasBusinessRole.

Read access (GET) is open to any business member (HasBusinessRole's
default STAFF+); writes — updating profile fields, uploading/removing the
logo — require IsBusinessManager (MANAGER+). Staff can see business
settings but not change them.
"""

from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Business
from core.permissions import HasBusinessRole, IsBusinessManager, business_ids_for_user
from documents import services as documents_services

from . import services
from .models import BusinessProfile
from .serializers import BusinessProfileSerializer, LogoUploadSerializer


def _get_business(user, business_id):
    business = Business.objects.filter(id=business_id, id__in=business_ids_for_user(user)).first()
    if business is None:
        raise NotFound("Business not found.")
    return business


class BusinessProfileView(generics.RetrieveUpdateAPIView):
    """
    One profile row per business, auto-created with defaults on first
    access — same pattern as reservations.views.ReservationSettingView.
    """

    serializer_class = BusinessProfileSerializer
    business_lookup_url_kwarg = "business_id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            return [IsBusinessManager()]
        return [HasBusinessRole()]

    def get_object(self):
        business = _get_business(self.request.user, self.kwargs["business_id"])
        profile, _created = BusinessProfile.objects.get_or_create(business=business)
        self.check_object_permissions(self.request, profile)
        return profile


class BusinessProfileLogoUploadView(APIView):
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def post(self, request, business_id=None):
        business = _get_business(request.user, business_id)
        profile, _created = BusinessProfile.objects.get_or_create(business=business)

        input_serializer = LogoUploadSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        uploaded_file = data["file"]

        try:
            profile = services.set_logo(
                profile,
                name=data["name"],
                file_obj=uploaded_file,
                content_type=uploaded_file.content_type or "",
                size=uploaded_file.size,
                uploaded_by=request.business_membership,
            )
        except documents_services.UploadFailedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(BusinessProfileSerializer(profile).data, status=status.HTTP_201_CREATED)


class BusinessProfileLogoRemoveView(APIView):
    permission_classes = [IsBusinessManager]
    business_lookup_url_kwarg = "business_id"

    def delete(self, request, business_id=None):
        business = _get_business(request.user, business_id)
        profile, _created = BusinessProfile.objects.get_or_create(business=business)
        profile = services.remove_logo(profile)
        return Response(BusinessProfileSerializer(profile).data)
