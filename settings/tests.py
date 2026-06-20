"""
Tests for the Settings domain: profile CRUD, role gating (staff read-only,
manager+ can write), logo upload reusing Documents' orphan-prevention
flow (proven the same way Documents' own tests prove it — mock the
storage call to fail and confirm no orphan, not just inferred from the
code path existing), and tenant isolation.
"""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from authentication.models import User
from core.models import Business, BusinessMembership
from documents.models import Document

from .models import BusinessProfile


def profile_url(business_id):
    return f"/api/businesses/{business_id}/profile/"


def upload_logo_url(business_id):
    return f"/api/businesses/{business_id}/profile/upload-logo/"


def remove_logo_url(business_id):
    return f"/api/businesses/{business_id}/profile/logo/"


def make_upload(name="logo.png", content=b"fake-png-bytes", content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class BusinessProfileCRUDTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner_profile@example.com")
        self.business = Business.objects.create(name="Profile Biz", owner=self.owner)
        self.manager_user = User.objects.create_user(email="manager_profile@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.manager_user, role=BusinessMembership.Role.MANAGER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.manager_user)

    def test_get_auto_creates_profile_with_defaults(self):
        response = self.client.get(profile_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["default_timezone"], "UTC")
        self.assertTrue(response.data["email_on_new_reservation"])
        self.assertEqual(BusinessProfile.objects.filter(business=self.business).count(), 1)

    def test_get_is_idempotent_does_not_create_duplicates(self):
        self.client.get(profile_url(self.business.id))
        self.client.get(profile_url(self.business.id))
        self.assertEqual(BusinessProfile.objects.filter(business=self.business).count(), 1)

    def test_manager_can_update_profile_fields(self):
        response = self.client.patch(
            profile_url(self.business.id),
            {"contact_email": "hello@business.example", "address": "123 Main St", "email_on_low_stock": False},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        profile = BusinessProfile.objects.get(business=self.business)
        self.assertEqual(profile.contact_email, "hello@business.example")
        self.assertEqual(profile.address, "123 Main St")
        self.assertFalse(profile.email_on_low_stock)

    def test_invalid_phone_rejected(self):
        response = self.client.patch(profile_url(self.business.id), {"contact_phone": "not-a-phone"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logo_field_cannot_be_set_directly(self):
        document = Document.objects.create(business=self.business, name="sneaky.png", storage_key="sneaky-key")
        response = self.client.patch(profile_url(self.business.id), {"logo": str(document.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        profile = BusinessProfile.objects.get(business=self.business)
        self.assertIsNone(profile.logo)


class RoleGatingTests(TestCase):
    def setUp(self):
        self.owner_user = User.objects.create_user(email="owner_role@example.com")
        self.business = Business.objects.create(name="Role Biz", owner=self.owner_user)
        BusinessMembership.objects.create(
            business=self.business, user=self.owner_user, role=BusinessMembership.Role.OWNER
        )
        self.manager_user = User.objects.create_user(email="manager_role@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.manager_user, role=BusinessMembership.Role.MANAGER
        )
        self.staff_user = User.objects.create_user(email="staff_role@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()

    def test_staff_can_read(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(profile_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_cannot_update(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.patch(profile_url(self.business.id), {"contact_email": "x@example.com"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_upload_logo(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(upload_logo_url(self.business.id), {"file": make_upload()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_remove_logo(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.delete(remove_logo_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_update(self):
        self.client.force_authenticate(user=self.manager_user)
        response = self.client.patch(profile_url(self.business.id), {"contact_email": "x@example.com"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_owner_can_update(self):
        self.client.force_authenticate(user=self.owner_user)
        response = self.client.patch(profile_url(self.business.id), {"contact_email": "owner@example.com"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class LogoUploadTests(TestCase):
    """
    Reuses documents.services.upload_document/delete_document directly —
    these tests confirm that reuse actually behaves correctly here, the
    same way documents.tests.DocumentUploadTests proves it for the
    Documents domain itself.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_logo@example.com")
        self.business = Business.objects.create(name="Logo Biz", owner=owner)
        self.manager_user = User.objects.create_user(email="manager_logo@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.manager_user, role=BusinessMembership.Role.MANAGER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.manager_user)

    @patch("documents.storage.upload_file")
    def test_successful_upload_sets_the_logo(self, mock_upload):
        response = self.client.post(upload_logo_url(self.business.id), {"file": make_upload()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        profile = BusinessProfile.objects.get(business=self.business)
        self.assertIsNotNone(profile.logo)
        self.assertEqual(profile.logo.status, Document.Status.UPLOADED)
        mock_upload.assert_called_once()

    @patch("documents.storage.upload_file", side_effect=Exception("storage is down"))
    def test_failed_upload_leaves_no_logo_and_no_orphan(self, mock_upload):
        response = self.client.post(upload_logo_url(self.business.id), {"file": make_upload()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

        profile = BusinessProfile.objects.get(business=self.business)
        self.assertIsNone(profile.logo)
        # Exactly the same guarantee documents.tests proves for Documents
        # directly: a failed upload produces one failed row, never an
        # orphaned file with no record.
        documents = Document.objects.filter(business=self.business)
        self.assertEqual(documents.count(), 1)
        self.assertEqual(documents.first().status, Document.Status.FAILED)

    @patch("documents.storage.delete_file")
    @patch("documents.storage.upload_file")
    def test_replacing_a_logo_deletes_the_old_document(self, mock_upload, mock_delete):
        first_response = self.client.post(
            upload_logo_url(self.business.id), {"file": make_upload(name="first.png")}, format="multipart"
        )
        old_logo_id = first_response.data["logo"]["id"]

        second_response = self.client.post(
            upload_logo_url(self.business.id), {"file": make_upload(name="second.png")}, format="multipart"
        )
        self.assertEqual(second_response.status_code, status.HTTP_201_CREATED, second_response.data)
        new_logo_id = second_response.data["logo"]["id"]

        self.assertNotEqual(old_logo_id, new_logo_id)
        mock_delete.assert_called_once()
        self.assertFalse(Document.objects.filter(id=old_logo_id).exists())
        self.assertTrue(Document.objects.filter(id=new_logo_id).exists())

    @patch("documents.storage.delete_file")
    @patch("documents.storage.upload_file")
    def test_replacement_upload_failure_does_not_touch_the_existing_logo(self, mock_upload, mock_delete):
        first_response = self.client.post(
            upload_logo_url(self.business.id), {"file": make_upload(name="keep-me.png")}, format="multipart"
        )
        kept_logo_id = first_response.data["logo"]["id"]

        mock_upload.side_effect = Exception("storage is down")
        second_response = self.client.post(
            upload_logo_url(self.business.id), {"file": make_upload(name="broken.png")}, format="multipart"
        )
        self.assertEqual(second_response.status_code, status.HTTP_502_BAD_GATEWAY)

        profile = BusinessProfile.objects.get(business=self.business)
        self.assertEqual(str(profile.logo_id), kept_logo_id)
        mock_delete.assert_not_called()

    @patch("documents.storage.delete_file")
    @patch("documents.storage.upload_file")
    def test_remove_logo_clears_profile_and_deletes_document(self, mock_upload, mock_delete):
        upload_response = self.client.post(
            upload_logo_url(self.business.id), {"file": make_upload()}, format="multipart"
        )
        logo_id = upload_response.data["logo"]["id"]

        response = self.client.delete(remove_logo_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["logo"])
        mock_delete.assert_called_once()
        self.assertFalse(Document.objects.filter(id=logo_id).exists())

    def test_remove_logo_when_none_set_is_a_no_op(self):
        response = self.client.delete(remove_logo_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["logo"])


class SettingsTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_settings_iso@example.com")
        self.business_a = Business.objects.create(name="Settings Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Settings Biz B", owner=owner)

        self.manager_a = User.objects.create_user(email="manager_settings_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.manager_a, role=BusinessMembership.Role.MANAGER
        )
        other_manager_b = User.objects.create_user(email="manager_settings_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_manager_b, role=BusinessMembership.Role.MANAGER
        )
        self.profile_b = BusinessProfile.objects.create(business=self.business_b, contact_email="b@example.com")

        self.client = APIClient()
        self.client.force_authenticate(user=self.manager_a)

    def test_cannot_read_other_business_profile(self):
        response = self.client.get(profile_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_update_other_business_profile(self):
        response = self.client.patch(profile_url(self.business_b.id), {"contact_email": "hacked@example.com"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.profile_b.refresh_from_db()
        self.assertEqual(self.profile_b.contact_email, "b@example.com")

    @patch("documents.storage.upload_file")
    def test_cannot_upload_logo_to_other_business(self, mock_upload):
        response = self.client.post(
            upload_logo_url(self.business_b.id), {"file": make_upload()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_upload.assert_not_called()

    def test_cannot_remove_other_business_logo(self):
        response = self.client.delete(remove_logo_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
