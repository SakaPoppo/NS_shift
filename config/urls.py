from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("staff/", include("staff.urls")),
    path("shifts/", include("shifts.urls")),
    path("", include("core.urls")),
]
