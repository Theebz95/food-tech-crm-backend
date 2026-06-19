"""
Documents domain.

  documents (old) -> Document

The actual fix (Phase 1 audit finding): the old flow uploaded straight to
Supabase Storage from the browser, then made a separate call to insert the
metadata row — if that second call failed, the file was already sitting in
storage with no database record of it at all (an orphan with zero trace,
discoverable only by walking the bucket directly).

Fix chosen: **write the DB row first, in a `pending` state, then upload,
then mark it `uploaded`** (`documents/services.py:upload_document`) —
rather than uploading first and trying to roll back the storage write if
the DB insert fails. Reasoning: the DB row always exists before any file
does, so a failed upload can only ever produce a `failed` row pointing at
a storage key that was never actually written — never a real file with no
record. The alternative (upload-then-rollback) requires a compensating
delete that can itself fail, which reintroduces exactly the orphan risk
this is meant to close. See README "Documents domain" for the full
writeup, including the (much narrower, documented) residual case where the
upload succeeds but the final "mark complete" write doesn't.
"""

import uuid

from django.db import models

from core.models import Business, BusinessMembership


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        UPLOADED = "uploaded", "Uploaded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=255)
    # The object key in Supabase Storage (S3-compatible) — see
    # documents/storage.py. Generated server-side in services.upload_document,
    # never client-supplied.
    storage_key = models.CharField(max_length=512, unique=True, editable=False)
    content_type = models.CharField(max_length=128, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    uploaded_by = models.ForeignKey(
        BusinessMembership, on_delete=models.SET_NULL, null=True, related_name="uploaded_documents"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"
