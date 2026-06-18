from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/finance/", include("finance.urls")),
    path("api/", include("customers.urls")),
    # Additional app urls.py files get wired in here as each domain is
    # built out in follow-up sessions.
]
