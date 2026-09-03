from django.urls import path

from .views import (
    BulkStaffCreateConfirmView,
    BulkStaffSetupView,
    StaffMemberCreateView,
    StaffMemberDeleteView,
    StaffMemberListView,
    StaffMemberUpdateView,
)

app_name = "staff"

urlpatterns = [
    path("", StaffMemberListView.as_view(), name="list"),
    path("create/", StaffMemberCreateView.as_view(), name="create"),
    path("bulk-create/", BulkStaffSetupView.as_view(), name="bulk_create"),
    path("bulk-create/confirm/", BulkStaffCreateConfirmView.as_view(), name="bulk_create_confirm"),
    path("<int:pk>/edit/", StaffMemberUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", StaffMemberDeleteView.as_view(), name="delete"),
]
