"""
Field-level encryption at rest for OAuth tokens (GoogleAdsCampaign).

Plain CharField/TextField storage would mean anyone with read access to
the database — a backup, a replica, a leaked connection string, a
careless `SELECT *` in a support tool — gets a directly usable OAuth
token. EncryptedTextField encrypts with Fernet (AES-128-CBC + HMAC,
authenticated so tampering is detected, not just hidden) before the value
is ever sent to Postgres, and decrypts transparently on read, so the
model/serializer code reads like a normal TextField.

Keyed by a dedicated `FIELD_ENCRYPTION_KEY` setting — deliberately not
reusing `SECRET_KEY` or `SUPABASE_JWT_SECRET`, so rotating one doesn't
entangle the other. This is the same pattern `django-cryptography` /
`django-fernet-fields` implement as a library; written by hand here since
it's a single field on a single model and doesn't justify a new
dependency on top of `cryptography` itself.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _get_fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


class EncryptedTextField(models.TextField):
    """Transparently encrypts on write, decrypts on read. NULL/blank pass through unchanged."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            # Ciphertext that doesn't decrypt with the current key (wrong
            # key, corrupted column, tampered value) — surface as empty
            # rather than raising and breaking every read of the row.
            return ""
