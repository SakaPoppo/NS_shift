from django.contrib import admin

from .models import ShiftPlan


@admin.register(ShiftPlan)
class ShiftPlanAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "year", "month", "title", "status", "created_at")
    list_filter = ("status", "year", "month")
    search_fields = ("title", "user__username")
