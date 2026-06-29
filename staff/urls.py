from django.urls import path

from .views import StaffMemberListView

app_name = "staff"

urlpatterns = [
    path("", StaffMemberListView.as_view(), name="list"),
]
