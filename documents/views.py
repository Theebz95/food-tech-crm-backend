"""
Document endpoints, tenant-scoped via core.permissions.HasBusinessRole.

Upload and delete are explicit, custom-implemented actions through
documents/services.py rather than generic ModelViewSet create/destroy —
the order of operations (DB row before storage write on upload, storage
delete before DB row on delete) is the actual fix this domain exists for,
so it can't be left to a generic serializer.save()/instance.delete() call.
"""

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.models import Business
from core.permissions import HasBusinessRole, business_ids_for_user

from . import services, storage
from .models import Document
from .serializers import DocumentSerializer, DocumentUploadSerializer


class DocumentViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    serializer_class = DocumentSerializer
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
        return Document.objects.filter(business_id=self.kwargs["business_id"])

    def create(self, request, business_id=None):
        input_serializer = DocumentUploadSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        uploaded_file = data["file"]
        try:
            document = services.upload_document(
                business=self.get_business(),
                name=data["name"],
                file_obj=uploaded_file,
                content_type=uploaded_file.content_type or "",
                size=uploaded_file.size,
                uploaded_by=request.business_membership,
            )
        except services.UploadFailedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, business_id=None, pk=None):
        document = self.get_object()
        services.delete_document(document)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def download(self, request, business_id=None, pk=None):
        document = self.get_object()
        if document.status != Document.Status.UPLOADED:
            return Response(
                {"detail": "Document is not available for download."}, status=status.HTTP_400_BAD_REQUEST
            )
        url = storage.get_presigned_url(document.storage_key)
        return Response({"url": url})
