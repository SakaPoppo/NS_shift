from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("staff/", include("staff.urls")),
    path("shifts/", include("shifts.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
