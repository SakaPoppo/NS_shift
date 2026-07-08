from django.urls import path

from .views import ShiftPlanCreateView, ShiftPlanListView

app_name = "shifts"

urlpatterns = [
    path("", ShiftPlanListView.as_view(), name="list"),
    path("create/", ShiftPlanCreateView.as_view(), name="create"),
]
