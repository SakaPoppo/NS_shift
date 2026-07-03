from django.urls import path

from .views import ShiftPlanListView

app_name = "shifts"

urlpatterns = [
    path("", ShiftPlanListView.as_view(), name="list"),
]
