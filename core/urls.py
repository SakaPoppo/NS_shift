from django.urls import path

from .views import TopPageView

app_name = "core"

urlpatterns = [
    path("", TopPageView.as_view(), name="top_page"),
]
