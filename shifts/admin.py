from django.contrib import admin

from .models import DayOffRequest, ShiftPlan, ShiftResult, ShiftRule


@admin.register(ShiftPlan)
class ShiftPlanAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "year", "month", "title", "status", "created_at")
    list_filter = ("status", "year", "month")
    search_fields = ("title", "user__username")


@admin.register(ShiftRule)
class ShiftRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "shift_plan",
        "off_days_per_staff",
        "max_consecutive_work_days",
        "required_day_staff",
        "required_night_staff",
    )


@admin.register(DayOffRequest)
class DayOffRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "shift_plan", "staff_member", "date", "created_at")
    list_filter = ("date",)
    search_fields = ("staff_member__name", "shift_plan__title")


@admin.register(ShiftResult)
class ShiftResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "shift_plan",
        "staff_member",
        "date",
        "shift_type",
        "input_type",
        "is_locked",
    )
    list_filter = ("shift_type", "input_type", "is_locked", "date")
    search_fields = ("staff_member__name", "shift_plan__title")
