from django.urls import path

from .views import MainPageView, TopPageView, health_check

app_name = "core"

urlpatterns = [
    path("healthz/", health_check, name="health_check"),
    path("main/", MainPageView.as_view(), name="main_page"),
    path("", TopPageView.as_view(), name="top_page"),
]
