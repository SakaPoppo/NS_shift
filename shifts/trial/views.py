"""未ログインで利用できる、お試し版シフト生成画面。"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from shifts.services import get_japanese_holiday_dates
from shifts.shift_edit import (
    apply_night_shift_sequences,
    parse_submitted_assignments,
    validate_manual_assignments,
)
from shifts.shift_generation.client import generate_with_optimizer_api
from shifts.shift_generation.markers import build_generation_issue_markers
from shifts.shift_generation.types import ShiftGenerationError
from shifts.views import (
    SHIFT_SELECT_OPTIONS,
    add_generation_issue_message,
    build_day_headers,
    build_shift_plan_grid,
)

from .context import build_trial_generation_context
from .rate_limit import (
    consume_trial_generation_quota,
    get_trial_visitor_id,
    release_trial_generation_lock,
)
from .state import clear_trial_state, load_trial_state, save_trial_state


class ShiftPlanTrialView(View):
    """固定サンプルとSession状態だけで動作するシフト生成体験版。"""

    template_name = "shifts/shift_plan_edit.html"
    allowed_actions = {"save", "generate", "reset_to_manual", "reset_to_base"}

    def get_reference_date(self):
        return timezone.localdate()

    def get(self, request, *args, **kwargs):
        get_trial_visitor_id(request.session)
        context = build_trial_generation_context(self.get_reference_date())
        state = load_trial_state(request.session, context)
        return render(request, self.template_name, self.build_page_context(context, state))

    def post(self, request, *args, **kwargs):
        get_trial_visitor_id(request.session)
        reference_date = self.get_reference_date()
        context = build_trial_generation_context(reference_date)
        state = load_trial_state(request.session, context)
        action = request.POST.get("action", "save")

        if action not in self.allowed_actions:
            return HttpResponseBadRequest("Invalid trial action.")

        if action == "reset_to_manual":
            state["generated_assignments"] = {}
            state["generation_issues"] = []
            save_trial_state(request.session, context, state)
            messages.success(request, "自動生成した勤務を削除し、保存した入力内容へ戻しました。")
            return redirect("shifts:trial")

        if action == "reset_to_base":
            clear_trial_state(request.session)
            messages.success(request, "体験版の入力内容を初期状態へ戻しました。")
            return redirect("shifts:trial")

        base_fixed_assignments = self.get_base_fixed_assignments(context)
        submitted_assignments = parse_submitted_assignments(
            request.POST,
            context.staff_members,
            context.month_dates,
            base_fixed_assignments,
        )
        _, night_after_conflicts = apply_night_shift_sequences(
            None,
            context.month_dates,
            submitted_assignments,
            base_fixed_assignments,
            {},
            shift_rule=context.shift_rule,
        )
        validation_errors = validate_manual_assignments(
            None,
            context.staff_members,
            context.month_dates,
            submitted_assignments,
            base_fixed_assignments,
            {},
            night_after_conflicts,
            previous_shift_types={},
            shift_rule=context.shift_rule,
        )
        if validation_errors:
            for error in validation_errors:
                messages.error(request, error)
            return render(
                request,
                self.template_name,
                self.build_page_context(
                    context,
                    state,
                    display_assignments=submitted_assignments,
                    generation_issues=[],
                ),
            )

        saved_assignments = self.build_saved_assignments(
            submitted_assignments,
            state["saved_assignments"],
            state["generated_assignments"],
        )
        if action == "generate":
            quota_result = consume_trial_generation_quota(request)
            if not quota_result.allowed:
                return self.render_rate_limited_response(
                    request,
                    context,
                    state,
                    submitted_assignments,
                    quota_result,
                )
            return self.generate(
                request,
                reference_date,
                context,
                state,
                saved_assignments,
                quota_result.generation_started_at,
            )

        state["saved_assignments"] = saved_assignments
        state["generated_assignments"] = {}
        state["generation_issues"] = []
        save_trial_state(request.session, context, state)
        messages.success(request, "入力内容を一時保存しました。")
        return redirect("shifts:trial")

    def render_rate_limited_response(
        self,
        request,
        context,
        state,
        display_assignments,
        rate_limit_result,
    ):
        messages.error(request, rate_limit_result.message)
        response = render(
            request,
            self.template_name,
            self.build_page_context(
                context,
                state,
                display_assignments=display_assignments,
                generation_issues=[],
            ),
            status=429,
        )
        return response

    def generate(
        self,
        request,
        reference_date,
        context,
        state,
        saved_assignments,
        generation_started_at,
    ):
        try:
            generation_context = build_trial_generation_context(
                reference_date,
                manual_assignments=saved_assignments,
            )
            generation_result = generate_with_optimizer_api(generation_context)
        except ShiftGenerationError as error:
            issues = getattr(error, "issues", [error.issue])
            for issue in issues:
                add_generation_issue_message(request, issue)
            return render(
                request,
                self.template_name,
                self.build_page_context(
                    context,
                    state,
                    display_assignments=saved_assignments,
                    generation_issues=issues,
                ),
            )
        else:
            state["saved_assignments"] = saved_assignments
            state["generated_assignments"] = {
                (shift.staff_member_id, shift.date): shift.shift_type
                for shift in generation_result.shifts
            }
            state["generation_issues"] = generation_result.issues
            save_trial_state(request.session, context, state)
            for issue in generation_result.issues:
                add_generation_issue_message(request, issue)
            messages.success(request, "体験版のシフトを生成しました。")
            return redirect("shifts:trial")
        finally:
            release_trial_generation_lock(request, generation_started_at)

    def build_page_context(
        self,
        context,
        state,
        *,
        display_assignments=None,
        generation_issues=None,
    ):
        if display_assignments is None:
            display_assignments = (
                state["generated_assignments"] or state["saved_assignments"]
            )
        if generation_issues is None:
            generation_issues = state["generation_issues"]

        base_fixed_assignments = self.get_base_fixed_assignments(context)
        issue_markers = build_generation_issue_markers(generation_issues)
        first_date = context.month_dates[0]
        day_headers = build_day_headers(
            context.month_dates,
            get_japanese_holiday_dates(first_date.year, first_date.month),
            issue_markers.date_issue_levels,
            issue_markers.date_issue_titles,
        )
        staff_rows, day_summary_rows = build_shift_plan_grid(
            context.staff_members,
            context.month_dates,
            {},
            base_fixed_assignments,
            display_assignments=display_assignments,
            daily_summary_issue_levels=issue_markers.daily_summary_issue_levels,
            daily_summary_issue_titles=issue_markers.daily_summary_issue_titles,
            cell_issue_levels=issue_markers.cell_issue_levels,
            cell_issue_titles=issue_markers.cell_issue_titles,
            staff_summary_issue_levels=issue_markers.staff_summary_issue_levels,
            staff_summary_issue_titles=issue_markers.staff_summary_issue_titles,
        )
        return {
            "page_title": f"{first_date.year}年{first_date.month}月 シフト生成体験版",
            "shift_rule": context.shift_rule,
            "staff_rows": staff_rows,
            "month_dates": context.month_dates,
            "day_headers": day_headers,
            "day_summary_rows": day_summary_rows,
            "shift_select_options": SHIFT_SELECT_OPTIONS,
            "generation_target_count": len(context.staff_members),
            "excluded_staff_count": 0,
            "weekday_rule_count": 0,
            "date_rule_count": 0,
            "missing_previous_staff_members": [],
            "can_save": True,
            "can_generate": True,
            "can_reset": True,
            "can_change_generation_targets": False,
            "can_edit_conditions": False,
            "can_export_csv": False,
            "show_disabled_export": True,
        }

    @staticmethod
    def get_base_fixed_assignments(context):
        return {
            cell_key: {"shift_type": shift_type, "source": "trial_fixed"}
            for cell_key, shift_type in context.fixed_assignments.items()
        }

    @staticmethod
    def build_saved_assignments(
        submitted_assignments,
        saved_assignments,
        generated_assignments,
    ):
        """生成済みの表示値は手入力として再保存しない。"""

        if not generated_assignments:
            return {
                cell_key: shift_type
                for cell_key, shift_type in submitted_assignments.items()
                if shift_type
            }

        assignments = dict(saved_assignments)
        for cell_key, shift_type in submitted_assignments.items():
            if shift_type == generated_assignments.get(cell_key, ""):
                continue
            if shift_type:
                assignments[cell_key] = shift_type
            else:
                assignments.pop(cell_key, None)
        return assignments
