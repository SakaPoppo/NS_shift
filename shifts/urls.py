from django.urls import path

from .views import (
    ShiftPlanCreateView,
    ShiftPlanDeleteView,
    ShiftPlanEditView,
    ShiftPlanListView,
)

app_name = "shifts"

urlpatterns = [
    path("", ShiftPlanListView.as_view(), name="list"),
    path("create/", ShiftPlanCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", ShiftPlanEditView.as_view(), name="edit"),
    path("<int:pk>/delete/", ShiftPlanDeleteView.as_view(), name="delete"),
]
