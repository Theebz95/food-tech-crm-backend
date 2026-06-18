from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """
    Supabase owns signup/password; rows here are created lazily by
    SupabaseAuthentication on first authenticated request. This view is
    read-mostly — useful for toggling is_superadmin and inspecting which
    Supabase users have synced into Django so far.
    """

    list_display = ("email", "is_superadmin", "is_staff", "is_active", "date_joined")
    list_filter = ("is_superadmin", "is_staff", "is_active")
    search_fields = ("email", "id")
    ordering = ("-date_joined",)
    readonly_fields = ("id", "date_joined", "last_login")
