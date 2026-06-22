"""
Reusable tenant-scoped access control, built on BusinessMembership.

This is the direct replacement for the old RLS pattern of
`USING (auth.uid() = user_id)`: instead of comparing a row's owner column to
the requesting user, we look up whether the user has an active
BusinessMembership on the relevant Business with a role at or above what the
view requires.
"""

from rest_framework.permissions import BasePermission

from .models import Business, BusinessMembership


def business_ids_for_user(user):
    """Queryset of Business IDs the given user can access at all (any role)."""
    if getattr(user, "is_superadmin", False):
        return Business.objects.values_list("id", flat=True)
    return BusinessMembership.objects.filter(user=user, is_active=True).values_list(
        "business_id", flat=True
    )


def get_membership(user, business_id):
    return BusinessMembership.objects.filter(
        business_id=business_id, user=user, is_active=True
    ).first()


def _business_is_active(business_id):
    """
    Business.is_active is written correctly (core.tasks.check_expired_trials,
    finance.webhooks' Stripe handler) but, before this check existed, was
    read by nothing — a deactivated/lapsed business's staff retained full
    API access regardless. This is the fix: every business-scoped request,
    not just the two writers, now respects the flag.
    """
    return Business.objects.filter(id=business_id, is_active=True).exists()


class HasBusinessRole(BasePermission):
    """
    Tenant-scoped permission for DRF views.

    Usage:
        class MyView(APIView):
            permission_classes = [HasBusinessRole]
            required_role = BusinessMembership.Role.MANAGER   # default: STAFF
            business_lookup_url_kwarg = "business_id"          # default

    `has_permission` checks the URL kwarg (list/create views); `has_object_permission`
    checks `obj.business_id` / `obj.business.id` (detail views). Superadmins
    bypass both checks, matching the old is_superadmin()-bypasses-RLS behavior.
    """

    required_role = BusinessMembership.Role.STAFF
    business_lookup_url_kwarg = "business_id"

    def _role_is_sufficient(self, membership, view):
        if membership is None:
            return False
        required_role = getattr(view, "required_role", self.required_role)
        return BusinessMembership.ROLE_RANK[membership.role] >= BusinessMembership.ROLE_RANK[required_role]

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if getattr(request.user, "is_superadmin", False):
            return True

        url_kwarg = getattr(view, "business_lookup_url_kwarg", self.business_lookup_url_kwarg)
        business_id = view.kwargs.get(url_kwarg)
        if not business_id:
            # No business in the URL (e.g. a list view scoped some other way) —
            # let the view's get_queryset() do its own filtering via
            # business_ids_for_user(); this permission class only blocks
            # cross-tenant access when a specific business_id is present.
            return True

        membership = get_membership(request.user, business_id)
        if membership is not None:
            request.business_membership = membership
        if not self._role_is_sufficient(membership, view):
            return False
        return _business_is_active(business_id)

    def has_object_permission(self, request, view, obj):
        if getattr(request.user, "is_superadmin", False):
            return True

        business_id = getattr(obj, "business_id", None)
        if business_id is None and hasattr(obj, "business"):
            business_id = obj.business.id
        if business_id is None:
            return False

        membership = get_membership(request.user, business_id)
        if not self._role_is_sufficient(membership, view):
            return False
        return _business_is_active(business_id)


class IsBusinessManager(HasBusinessRole):
    required_role = BusinessMembership.Role.MANAGER


class IsBusinessOwner(HasBusinessRole):
    required_role = BusinessMembership.Role.OWNER
