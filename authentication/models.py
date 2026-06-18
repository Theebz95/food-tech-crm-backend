"""
A single, unified User model for both business users and customer/portal
users. There is no separate customer_portal_accounts / portal_sessions
table or PBKDF2 password scheme — everyone is a normal Supabase Auth user,
distinguished by their BusinessMembership rows (core.models) and, once the
Customers domain is built, a CustomerProfile linked to this model.
"""

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        extra_fields.setdefault("id", uuid.uuid4())
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_superadmin", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Mirrors a Supabase Auth user. Django does not own signup, login, or
    password storage for these accounts — Supabase Auth is the source of
    truth there, and the frontend keeps talking to Supabase Auth directly
    (per the unified-auth decision). This table exists so Django has a
    normal FK target (BusinessMembership.user, Business.owner, etc.) and so
    DRF's `request.user` resolves to something.

    Rows are created lazily, on first authenticated request, by
    SupabaseAuthentication (see authentication/authentication.py), using
    the JWT's `sub` claim as the primary key — guaranteeing it always
    matches the Supabase auth.users.id the frontend already knows about.
    """

    id = models.UUIDField(primary_key=True, editable=False)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False, help_text="Grants Django admin access only.")
    is_superadmin = models.BooleanField(
        default=False,
        help_text=(
            "Platform-level superadmin flag. Replaces the old user_roles "
            "table + is_superadmin()/has_role() Postgres functions — "
            "'superadmin' was the only role ever checked outside the "
            "per-business membership system, so a single boolean here is "
            "sufficient (see authentication/permissions.py)."
        ),
    )
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email
