"""
Thin wrapper around Supabase Storage's S3-compatible API.

Deliberately not routed through Django's storage abstraction
(django-storages etc) — documents/services.py needs precise control over
the order of operations (DB row first, then upload — see models.py module
docstring for why), and a small set of plain functions here is trivially
mockable in tests ("mock the storage call to raise") without needing to
fake out Django's FileField/Storage machinery.
"""

import boto3
from django.conf import settings


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.SUPABASE_STORAGE_ENDPOINT_URL,
        aws_access_key_id=settings.SUPABASE_STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.SUPABASE_STORAGE_SECRET_ACCESS_KEY,
        region_name=settings.SUPABASE_STORAGE_REGION,
    )


def upload_file(storage_key, fileobj, content_type):
    _get_client().put_object(
        Bucket=settings.SUPABASE_STORAGE_BUCKET,
        Key=storage_key,
        Body=fileobj,
        ContentType=content_type or "application/octet-stream",
    )


def delete_file(storage_key):
    _get_client().delete_object(Bucket=settings.SUPABASE_STORAGE_BUCKET, Key=storage_key)


def get_presigned_url(storage_key, expires_in=3600):
    return _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.SUPABASE_STORAGE_BUCKET, "Key": storage_key},
        ExpiresIn=expires_in,
    )
