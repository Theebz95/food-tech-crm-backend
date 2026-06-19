"""
Upload/delete service layer — see models.py module docstring for the
orphan-prevention strategy this implements.
"""

import uuid

from . import storage
from .models import Document


class UploadFailedError(Exception):
    pass


def _build_storage_key(business, filename):
    return f"{business.id}/{uuid.uuid4()}-{filename}"


def upload_document(business, name, file_obj, content_type, size, uploaded_by) -> Document:
    storage_key = _build_storage_key(business, name)
    # The row exists, status=pending, before any storage call is made —
    # this is the actual fix. If the upload below fails, there is no file
    # in storage for this key at all, only this row (now marked failed),
    # so there's no possible orphan: every storage_key that ever gets
    # written is one this row already pointed at first.
    document = Document.objects.create(
        business=business,
        name=name,
        storage_key=storage_key,
        content_type=content_type,
        size=size,
        uploaded_by=uploaded_by,
        status=Document.Status.PENDING,
    )

    try:
        storage.upload_file(storage_key, file_obj, content_type)
    except Exception as exc:
        document.status = Document.Status.FAILED
        document.save(update_fields=["status", "updated_at"])
        raise UploadFailedError(f"Failed to upload {name!r} to storage.") from exc

    document.status = Document.Status.UPLOADED
    document.save(update_fields=["status", "updated_at"])
    return document


def delete_document(document: Document) -> None:
    """
    Storage first, then the DB row. If the storage delete fails, the row
    survives — a retryable, visible state, not a silent orphan. Deleting
    the row first and having the storage delete fail afterward would
    leave an untracked file with the record already gone, which is the
    same class of bug this domain exists to avoid, just in reverse.
    """
    storage.delete_file(document.storage_key)
    document.delete()
