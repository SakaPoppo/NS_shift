from django.urls import path

from .views import StaffMemberCreateView, StaffMemberListView

app_name = "staff"

urlpatterns = [
    path("", StaffMemberListView.as_view(), name="list"),
]
