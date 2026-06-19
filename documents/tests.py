"""
Tests for the Documents domain: the upload orphan-prevention fix (proven
by actually mocking the storage call to fail, not just inferred from the
ordering of operations in code), delete removing both the file and the
row, and tenant isolation (same rigor as prior domains).
"""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership

from . import services
from .models import Document


def document_list_url(business_id):
    return f"/api/businesses/{business_id}/documents/"


def document_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/documents/{pk}/"


def document_download_url(business_id, pk):
    return f"/api/businesses/{business_id}/documents/{pk}/download/"


def make_upload(name="report.txt", content=b"hello world", content_type="text/plain"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class DocumentUploadTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_doc@example.com")
        self.business = Business.objects.create(name="Docs Biz", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff_doc@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    @patch("documents.storage.upload_file")
    def test_successful_upload_marks_document_uploaded(self, mock_upload):
        response = self.client.post(
            document_list_url(self.business.id), {"file": make_upload()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        document = Document.objects.get(id=response.data["id"])
        self.assertEqual(document.status, Document.Status.UPLOADED)
        self.assertEqual(document.business_id, self.business.id)
        mock_upload.assert_called_once()
        called_key = mock_upload.call_args.args[0]
        self.assertEqual(called_key, document.storage_key)

    @patch("documents.storage.upload_file", side_effect=Exception("storage is down"))
    def test_failed_upload_leaves_a_failed_row_not_a_phantom_file(self, mock_upload):
        response = self.client.post(
            document_list_url(self.business.id), {"file": make_upload()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

        # Exactly one row, clearly marked failed — not silently deleted,
        # not left stuck on "pending" forever, and no exception escaping
        # to a 500.
        documents = Document.objects.all()
        self.assertEqual(documents.count(), 1)
        self.assertEqual(documents.first().status, Document.Status.FAILED)

    def test_failed_upload_at_the_service_layer_directly(self):
        # Same fix, exercised one layer down: prove upload_document itself
        # never leaves storage referencing a row that doesn't exist (or
        # vice versa) when the storage write raises.
        with patch("documents.storage.upload_file", side_effect=ConnectionError("boom")):
            with self.assertRaises(services.UploadFailedError):
                services.upload_document(
                    business=self.business,
                    name="x.txt",
                    file_obj=make_upload(),
                    content_type="text/plain",
                    size=11,
                    uploaded_by=None,
                )
        document = Document.objects.get(name="x.txt")
        self.assertEqual(document.status, Document.Status.FAILED)
        # The row was created (with its storage_key) before the upload was
        # ever attempted — that ordering is what makes "no DB record" for
        # a real file structurally impossible here.
        self.assertTrue(document.storage_key)


class DocumentDeleteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_doc_delete@example.com")
        self.business = Business.objects.create(name="Delete Docs Biz", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff_doc_delete@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        with patch("documents.storage.upload_file"):
            self.document = services.upload_document(
                business=self.business,
                name="to-delete.txt",
                file_obj=make_upload(),
                content_type="text/plain",
                size=11,
                uploaded_by=None,
            )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    @patch("documents.storage.delete_file")
    def test_delete_removes_both_storage_object_and_db_row(self, mock_delete):
        response = self.client.delete(document_detail_url(self.business.id, self.document.id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_delete.assert_called_once_with(self.document.storage_key)
        self.assertFalse(Document.objects.filter(id=self.document.id).exists())

    def test_delete_failure_leaves_the_row_intact_not_orphaned_in_reverse(self):
        with patch("documents.storage.delete_file", side_effect=Exception("storage unreachable")):
            with self.assertRaises(Exception):
                services.delete_document(self.document)
        # The row must still exist — losing the record while the storage
        # delete failed would itself be an orphan, just the other way
        # around (an untracked file with no row pointing at it).
        self.assertTrue(Document.objects.filter(id=self.document.id).exists())


class DocumentDownloadTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_doc_download@example.com")
        self.business = Business.objects.create(name="Download Docs Biz", owner=self.owner)
        self.staff_user = User.objects.create_user(email="staff_doc_download@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        with patch("documents.storage.upload_file"):
            self.document = services.upload_document(
                business=self.business,
                name="downloadable.txt",
                file_obj=make_upload(),
                content_type="text/plain",
                size=11,
                uploaded_by=None,
            )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    @patch("documents.storage.get_presigned_url", return_value="https://example.com/signed-url")
    def test_download_returns_a_presigned_url(self, mock_presign):
        response = self.client.get(document_download_url(self.business.id, self.document.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "https://example.com/signed-url")
        mock_presign.assert_called_once_with(self.document.storage_key)

    def test_cannot_download_a_failed_document(self):
        self.document.status = Document.Status.FAILED
        self.document.save(update_fields=["status"])
        response = self.client.get(document_download_url(self.business.id, self.document.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DocumentTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_doc_iso@example.com")
        self.business_a = Business.objects.create(name="Doc Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Doc Biz B", owner=owner)

        self.user_a = User.objects.create_user(email="staff_doc_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_doc_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        with patch("documents.storage.upload_file"):
            self.document_b = services.upload_document(
                business=self.business_b,
                name="b-secret.txt",
                file_obj=make_upload(),
                content_type="text/plain",
                size=11,
                uploaded_by=None,
            )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_documents(self):
        response = self.client.get(document_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_retrieve_other_business_document(self):
        response = self.client.get(document_detail_url(self.business_b.id, self.document_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_download_other_business_document(self):
        response = self.client.get(document_download_url(self.business_b.id, self.document_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("documents.storage.delete_file")
    def test_cannot_delete_other_business_document(self, mock_delete):
        response = self.client.delete(document_detail_url(self.business_b.id, self.document_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_delete.assert_not_called()
        self.assertTrue(Document.objects.filter(id=self.document_b.id).exists())

    @patch("documents.storage.upload_file")
    def test_cannot_upload_to_other_business(self, mock_upload):
        response = self.client.post(
            document_list_url(self.business_b.id), {"file": make_upload()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_upload.assert_not_called()
