"""
Logo upload/removal — thin wrappers around documents.services, not a
second upload flow. See models.py module docstring for why this domain
depends directly on documents.services rather than re-implementing the
write-row-first orphan-prevention pattern a second time.
"""

from documents import services as documents_services

from .models import BusinessProfile


def set_logo(profile: BusinessProfile, name, file_obj, content_type, size, uploaded_by) -> BusinessProfile:
    """
    Uploads the new logo first; if that fails (documents_services.UploadFailedError),
    it propagates immediately and `profile` is untouched — the existing
    logo, if any, stays exactly as it was. Only on success does the
    profile get pointed at the new Document, and only then is the old one
    (if any) deleted.
    """
    new_document = documents_services.upload_document(
        business=profile.business,
        name=name,
        file_obj=file_obj,
        content_type=content_type,
        size=size,
        uploaded_by=uploaded_by,
    )

    old_logo = profile.logo
    profile.logo = new_document
    profile.save(update_fields=["logo", "updated_at"])

    if old_logo is not None:
        documents_services.delete_document(old_logo)

    return profile


def remove_logo(profile: BusinessProfile) -> BusinessProfile:
    old_logo = profile.logo
    profile.logo = None
    profile.save(update_fields=["logo", "updated_at"])

    if old_logo is not None:
        documents_services.delete_document(old_logo)

    return profile
