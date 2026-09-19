"""シフト生成通知のユーザー向け文言を集約する。"""

from ..models import ShiftResult
from .types import GenerationIssue, GenerationIssueCode


ISSUE_TITLES = {
    GenerationIssueCode.SHIFT_GENERATED: "シフトを生成しました",
    GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED: "日勤人数を調整しました",
    GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED: "日勤人数が設定を下回っています",
    GenerationIssueCode.DAY_STAFFING_IMBALANCE: "日勤人数にばらつきがあります",
    GenerationIssueCode.NIGHT_COUNT_IMBALANCE: "夜勤回数にばらつきがあります",
    GenerationIssueCode.MONTHLY_OFF_COUNT_EXCEEDED: "月休日数が設定を超えています",
    GenerationIssueCode.OPTIMIZATION_INCOMPLETE: "一部の最適化を完了できませんでした",
    GenerationIssueCode.SHIFT_RULE_NOT_CONFIGURED: "シフト条件が未設定です",
    GenerationIssueCode.NO_ACTIVE_STAFF: "有効なスタッフがいません",
    GenerationIssueCode.NO_GENERATION_TARGET_STAFF: "生成対象のスタッフがいません",
    GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF: "夜勤人数を確保できません",
    GenerationIssueCode.INSUFFICIENT_LEADER_STAFF: "リーダー人数を確保できません",
    GenerationIssueCode.TOO_MANY_DAY_OFF_REQUESTS: "希望休が月休日数を超えています",
    GenerationIssueCode.NIGHT_SHIFT_NOT_ALLOWED: "夜勤設定が矛盾しています",
    GenerationIssueCode.NIGHT_SEQUENCE_CONFLICT: "夜勤後の勤務条件を満たせません",
    GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT: "勤務条件が競合しています",
    GenerationIssueCode.GENERATION_INFEASIBLE: "シフトを生成できません",
    GenerationIssueCode.OPTIMIZER_API_ERROR: "シフト最適化サービスでエラーが発生しました",
}

INCOMPLETE_ITEM_LABELS = {
    "day_ability_balance": "日勤能力配置",
    "night_ability_balance": "夜勤能力配置",
    "long_streak": "連勤配置",
}

SHIFT_TYPE_LABELS = dict(ShiftResult.ShiftTypeChoices.choices)


def format_generation_issue(issue: GenerationIssue) -> tuple[str, str]:
    """通知コードと詳細から、画面表示用のタイトル・本文を返す。"""

    title = ISSUE_TITLES.get(issue.code, "シフト生成に関するお知らせ")
    details = issue.details
    if issue.code == GenerationIssueCode.SHIFT_GENERATED:
        return title, "設定した条件をもとにシフトを生成しました。"
    if issue.code == GenerationIssueCode.SHIFT_RULE_NOT_CONFIGURED:
        return title, "シフト生成条件を設定してから再度お試しください。"
    if issue.code == GenerationIssueCode.NO_ACTIVE_STAFF:
        return title, "有効なスタッフを1名以上登録してから再度お試しください。"
    if issue.code == GenerationIssueCode.NO_GENERATION_TARGET_STAFF:
        return title, "シフト生成対象のスタッフを1名以上選択してください。"
    if issue.code == GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED:
        return title, "勤務可能人数を考慮し、一部の日で設定人数より多く日勤を配置しています。"
    if issue.code == GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED:
        return title, "勤務条件の影響により、一部の日で設定した必要日勤人数を確保できませんでした。可能な範囲で配置しています。"
    if issue.code == GenerationIssueCode.DAY_STAFFING_IMBALANCE:
        return title, "固定勤務や勤務条件の影響により、日勤人数を十分に均等化できませんでした。可能な範囲で調整しています。"
    if issue.code == GenerationIssueCode.NIGHT_COUNT_IMBALANCE:
        difference = details.get("count_difference")
        return title, f"固定勤務などの影響により、スタッフ間の夜勤回数に最大{difference}回の差があります。必須条件は満たしています。"
    if issue.code == GenerationIssueCode.MONTHLY_OFF_COUNT_EXCEEDED:
        return title, "固定勤務や定休日のため、月休日数が設定の{}日より{}日多い{}日になっています。".format(
            details["configured_off_count"],
            details["excess_count"],
            details["actual_off_count"],
        )
    if issue.code == GenerationIssueCode.OPTIMIZATION_INCOMPLETE:
        labels = [
            INCOMPLETE_ITEM_LABELS.get(item, item)
            for item in details.get("incomplete_items", [])
        ]
        return title, "処理時間の上限に達したため、{}の調整を完了できませんでした。必須条件を満たしたシフトを表示しています。".format("・".join(labels))
    if issue.code == GenerationIssueCode.TOO_MANY_DAY_OFF_REQUESTS:
        return title, "{staff_name}さんは希望休を含めると月休日数{monthly_off_days}日を超えます。希望休の一部を「有給」または「特別休暇」に変更するか、月休日数を見直してください。".format(**details)
    if issue.code == GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF:
        target_date = issue.dates[0]
        return title, "{:%-m月%-d}日は夜勤{}名が必要ですが、配置可能なスタッフは{}名です。必要夜勤人数またはスタッフの夜勤可否を確認してください。".format(target_date, details["required_count"], details["available_count"])
    if issue.code == GenerationIssueCode.INSUFFICIENT_LEADER_STAFF:
        target_date = issue.dates[0]
        return title, "{:%-m月%-d}日は日勤リーダー{}名が必要ですが、配置可能なリーダーは{}名です。必要リーダー人数または固定勤務を確認してください。".format(target_date, details["required_count"], details["available_count"])
    if issue.code == GenerationIssueCode.NIGHT_SHIFT_NOT_ALLOWED:
        return title, "{staff_name}さんは夜勤不可に設定されていますが、{:%-m月%-d}日に夜勤が設定されています。".format(issue.dates[0], **details)
    if issue.code == GenerationIssueCode.NIGHT_SEQUENCE_CONFLICT:
        return title, "{staff_name}さんの{:%-m月%-d}日の夜勤と、その後の勤務設定が両立できません。該当日の勤務を確認してください。".format(issue.dates[0], **details)
    if issue.code == GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT:
        return title, "{staff_name}さんの{:%-m月%-d}日は、固定条件「{fixed_shift_type}」と保存済み勤務「{saved_shift_type}」が競合しています。該当日の勤務を確認してください。".format(
            issue.dates[0],
            **{
                **details,
                "fixed_shift_type": SHIFT_TYPE_LABELS.get(
                    details["fixed_shift_type"], details["fixed_shift_type"]
                ),
                "saved_shift_type": SHIFT_TYPE_LABELS.get(
                    details["saved_shift_type"], details["saved_shift_type"]
                ),
            },
        )
    if issue.code == GenerationIssueCode.OPTIMIZER_API_ERROR:
        return title, details.get("reason", "時間をおいて再度お試しください。")
    return title, details.get("reason", "設定した条件を確認して再度お試しください。")
