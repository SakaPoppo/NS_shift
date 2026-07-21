from django.contrib import admin

from .models import DateShiftRule, DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule


@admin.register(ShiftPlan)
class ShiftPlanAdmin(admin.ModelAdmin):
    # ShiftPlan を管理画面で一覧しやすくする設定。
    list_display = ("id", "user", "display_title", "year", "month", "status", "created_at")
    list_filter = ("status", "year", "month")
    search_fields = ("user__username", "=year", "=month")

    @admin.display(description="シフト表名")
    def display_title(self, obj):
        return obj.display_title


@admin.register(ShiftRule)
class ShiftRuleAdmin(admin.ModelAdmin):
    # 月共通ルールを管理画面で確認しやすくする設定。
    list_display = (
        "id",
        "shift_plan",
        "required_day_staff",
        "required_night_staff",
        "off_days_per_staff",
        "required_leader_staff",
        "max_consecutive_work_days",
        "night_shift_next_day_off",
    )


@admin.register(WeekdayShiftRule)
class WeekdayShiftRuleAdmin(admin.ModelAdmin):
    # 曜日条件を管理画面で確認しやすくする設定。
    list_display = (
        "shift_plan",
        "day_of_week",
        "required_day_staff",
        "required_night_staff",
        "required_leader_staff",
    )
    list_filter = ("day_of_week",)
    search_fields = ("=shift_plan__year", "=shift_plan__month", "memo")


@admin.register(DateShiftRule)
class DateShiftRuleAdmin(admin.ModelAdmin):
    # 特定日条件を管理画面で確認しやすくする設定。
    list_display = (
        "shift_plan",
        "target_date",
        "required_day_staff",
        "required_night_staff",
        "required_leader_staff",
    )
    list_filter = ("target_date",)
    search_fields = ("=shift_plan__year", "=shift_plan__month", "memo")


@admin.register(DayOffRequest)
class DayOffRequestAdmin(admin.ModelAdmin):
    # 希望休を管理画面で確認しやすくする設定。
    list_display = ("id", "shift_plan", "staff_member", "date", "created_at")
    list_filter = ("date",)
    search_fields = ("staff_member__name", "=shift_plan__year", "=shift_plan__month", "memo")


@admin.register(ShiftResult)
class ShiftResultAdmin(admin.ModelAdmin):
    # 勤務結果を管理画面で確認しやすくする設定。
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
    search_fields = ("staff_member__name", "=shift_plan__year", "=shift_plan__month", "memo")


@admin.register(ShiftCarryover)
class ShiftCarryoverAdmin(admin.ModelAdmin):
    list_display = (
        "shift_plan", "staff_member", "source", "previous_shift_plan",
        "previous_last_shift_type", "previous_consecutive_work_days",
    )
