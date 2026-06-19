from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/finance/", include("finance.urls")),
    path("api/", include("customers.urls")),
    path("api/", include("employees.urls")),
    path("api/", include("reservations.urls")),
    path("api/", include("inventory.urls")),
    path("api/", include("documents.urls")),
    # Public, unauthenticated guest-booking routes — deliberately mounted
    # under a distinct prefix rather than alongside the HasBusinessRole
    # routes above, so it's unmistakable at the routing level which
    # endpoints require no auth. See reservations/public_views.py.
    path("api/public/", include("reservations.public_urls")),
    # Additional app urls.py files get wired in here as each domain is
    # built out in follow-up sessions.
]
