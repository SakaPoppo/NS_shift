from django.urls import path

from .views import TopPageView, health_check

app_name = "core"

urlpatterns = [
    path("healthz/", health_check, name="health_check"),
    path("", TopPageView.as_view(), name="top_page"),
]
