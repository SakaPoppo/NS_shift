from django.contrib import admin

from .models import StaffMember


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "job", "role", "can_night_shift", "is_active")
    list_filter = ("job", "role", "can_night_shift", "is_active")
    search_fields = ("name", "user__username", "user__email")
