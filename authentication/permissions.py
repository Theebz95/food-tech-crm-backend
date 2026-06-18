from rest_framework.permissions import BasePermission


class IsSuperAdmin(BasePermission):
    """
    Replaces the old Postgres is_superadmin(uuid) RPC / has_role() function
    + user_roles table. Superadmin is now a single boolean flag on the User
    model (see authentication/models.py) rather than a separate roles
    table, since it's a platform-wide flag, not a per-business role —
    per-business roles are handled entirely by BusinessMembership
    (core/permissions.py).
    """

    message = "Superadmin privileges are required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "is_superadmin", False)
        )
