"""
Single, unified Supabase JWT authentication for DRF.

The frontend (Vercel) keeps using Supabase Auth client-side exactly as it
does today, and attaches the resulting session JWT as
`Authorization: Bearer <token>` on every API call. Django never issues its
own tokens and never sees a password — it only verifies the JWT Supabase
already signed. This is the one and only auth path: business users and
customer/portal users are both just Supabase Auth users now (no separate
PBKDF2/custom-session portal scheme).
"""

import jwt
from django.conf import settings
from rest_framework import authentication, exceptions

from .models import User


class SupabaseAuthentication(authentication.BaseAuthentication):
    """
    Verifies the token using the Supabase project's JWT secret (HS256).
    If/when the Supabase project moves to asymmetric (RS256/ES256) signing
    keys, swap this for JWKS-based verification — the rest of the contract
    (get_or_create a User from `sub`/`email`) stays the same.
    """

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request).decode("utf-8")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

        try:
            payload = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience=settings.SUPABASE_JWT_AUDIENCE,
                options={"require": ["exp", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token has expired.")
        except jwt.InvalidTokenError as exc:
            raise exceptions.AuthenticationFailed(f"Invalid token: {exc}")

        user_id = payload.get("sub")
        email = payload.get("email", "")
        if not user_id:
            raise exceptions.AuthenticationFailed("Token missing 'sub' claim.")

        user, _created = User.objects.get_or_create(
            id=user_id,
            defaults={"email": email or f"{user_id}@unknown.local"},
        )
        if email and user.email != email:
            # Keep email in sync in case it changed on the Supabase side.
            user.email = email
            user.save(update_fields=["email"])

        if not user.is_active:
            raise exceptions.AuthenticationFailed("User is inactive.")

        return (user, payload)

    def authenticate_header(self, request):
        return 'Bearer realm="api"'
