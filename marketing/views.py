"""
Marketing/Analytics staff-side endpoints, tenant-scoped via
core.permissions.HasBusinessRole — same pattern as every other domain.
See public_views.py for the public, unauthenticated tracking/form
endpoints, which use a deliberately different permission model (there is
no User/BusinessMembership for an anonymous website visitor).
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import services
from .models import FormSubmission, GoogleAdsCampaign, Lead, PageView, TrackingEvent, TrackingScript, WebsiteVisitor
from .serializers import (
    FormSubmissionSerializer,
    GoogleAdsCampaignSerializer,
    LeadSerializer,
    PageViewSerializer,
    TrackingEventSerializer,
    TrackingScriptSerializer,
    WebsiteVisitorSerializer,
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


class _BusinessScopedReadOnlyViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [HasBusinessRole]
    business_lookup_url_kwarg = "business_id"


class TrackingScriptViewSet(_BusinessScopedViewSet):
    serializer_class = TrackingScriptSerializer

    def get_queryset(self):
        return TrackingScript.objects.filter(business_id=self.kwargs["business_id"])

    def perform_create(self, serializer):
        serializer.save(business=self.get_business(), script_key=services.generate_script_key())

    @action(detail=True, methods=["post"], url_path="regenerate-key")
    def regenerate_key(self, request, business_id=None, pk=None):
        script = self.get_object()
        script.script_key = services.generate_script_key()
        script.save(update_fields=["script_key", "updated_at"])
        return Response(TrackingScriptSerializer(script).data)


class WebsiteVisitorViewSet(_BusinessScopedReadOnlyViewSet):
    serializer_class = WebsiteVisitorSerializer

    def get_queryset(self):
        return WebsiteVisitor.objects.filter(business_id=self.kwargs["business_id"])


class PageViewViewSet(_BusinessScopedReadOnlyViewSet):
    serializer_class = PageViewSerializer

    def get_queryset(self):
        return PageView.objects.filter(visitor__business_id=self.kwargs["business_id"])


class TrackingEventViewSet(_BusinessScopedReadOnlyViewSet):
    serializer_class = TrackingEventSerializer

    def get_queryset(self):
        return TrackingEvent.objects.filter(visitor__business_id=self.kwargs["business_id"])


class LeadViewSet(_BusinessScopedViewSet):
    serializer_class = LeadSerializer

    def get_queryset(self):
        return Lead.objects.filter(business_id=self.kwargs["business_id"])


class FormSubmissionViewSet(_BusinessScopedReadOnlyViewSet):
    serializer_class = FormSubmissionSerializer

    def get_queryset(self):
        return FormSubmission.objects.filter(business_id=self.kwargs["business_id"])


class GoogleAdsCampaignViewSet(_BusinessScopedViewSet):
    serializer_class = GoogleAdsCampaignSerializer

    def get_queryset(self):
        return GoogleAdsCampaign.objects.filter(business_id=self.kwargs["business_id"])
