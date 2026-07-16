from django import forms
from django.utils import timezone

from staff.models import StaffMember

from .models import DateShiftRule, ShiftPlan, ShiftRule, WeekdayShiftRule
from .services import get_month_date_range


def coerce_bool(value):
    return value in {True, "True", "true", "1", "on"}


class ShiftPlanCreateForm(forms.ModelForm):
    START_YEAR = 2025
    YEAR_COUNT = 10
    YEAR_CHOICES = [(year, f"{year}年") for year in range(START_YEAR, START_YEAR + YEAR_COUNT)]
    MONTH_CHOICES = [(month, f"{month}月") for month in range(1, 13)]

    year = forms.TypedChoiceField( #数値入力欄は嫌だから自分でフォーム書くとこ１
        label="年",
        choices=YEAR_CHOICES,
        coerce=int, #数値に変換
    )
    month = forms.TypedChoiceField( #数値〜フォーム書くとこ2
        label="月",
        choices=MONTH_CHOICES,
        coerce=int, #数値に変換
        widget=forms.HiddenInput, #HiddenInputはHTML上で表示されない入力欄を作る
    )

    class Meta: #どのモデル、フィールドを使うかを指定
        model = ShiftPlan
        fields = ("title", "year", "month")

    def __init__(self, *args, user=None, **kwargs): #ユーザーはフォーム入力されないので、自力で引っ張ってくる
        self.user = user
        super().__init__(*args, **kwargs)

        today = timezone.localdate()
        year_choices = [choice[0] for choice in self.YEAR_CHOICES]
        initial_year = today.year if today.year in year_choices else year_choices[0]

        self.fields["title"].required = False
        self.fields["title"].widget.attrs.update(
            {
                "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none",
                "placeholder": "例：2026年7月 ICUシフト表",
            }
        )
        self.fields["year"].initial = self.initial.get("year", initial_year)
        self.fields["year"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["month"].initial = self.initial.get("month", today.month)

    def clean(self):
        cleaned_data = super().clean()
        year = cleaned_data.get("year")
        month = cleaned_data.get("month")
        if self.user and year and month:
            if ShiftPlan.objects.filter(user=self.user, year=year, month=month).exists():
                self.add_error("month", "選択した年月のシフト表はすでに作成されています。")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.title:
            instance.title = f"{instance.year}年{instance.month}月 シフト表"
        if commit:
            instance.save()
        return instance


class ShiftRuleForm(forms.Form):
    required_day_staff = forms.IntegerField(
        label="必要日勤数",
        min_value=0,
    )
    required_night_staff = forms.IntegerField(
        label="必要夜勤数",
        min_value=0,
    )
    off_days_per_staff = forms.IntegerField(
        label="月休日数",
        min_value=0,
    )
    required_leader_staff = forms.IntegerField(
        label="各勤務に必要なリーダークラス",
        min_value=0,
        required=False,
    )
    max_consecutive_work_days = forms.IntegerField(
        label="最大連勤数",
        min_value=1,
    )
    night_shift_next_day_off = forms.TypedChoiceField(
        label="夜勤明け翌日を公休にするか",
        choices=((True, "する"), (False, "しない")),
        coerce=coerce_bool,
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, shift_rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_rule = shift_rule

        for name, field in self.fields.items():
            if name == "night_shift_next_day_off":
                field.widget.attrs.update({"class": "radio radio-primary radio-sm"})
                continue

            field.widget.attrs.update(
                {
                    "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
                }
            )

        if shift_rule and not self.is_bound:
            self.initial.update(
                {
                    "required_day_staff": shift_rule.required_day_staff,
                    "required_night_staff": shift_rule.required_night_staff,
                    "off_days_per_staff": shift_rule.off_days_per_staff,
                    "required_leader_staff": shift_rule.required_leader_staff,
                    "max_consecutive_work_days": shift_rule.max_consecutive_work_days,
                    "night_shift_next_day_off": shift_rule.night_shift_next_day_off,
                }
            )
        elif not self.is_bound:
            self.initial.update(
                {
                    "required_day_staff": 0,
                    "required_night_staff": 0,
                    "off_days_per_staff": 0,
                    "required_leader_staff": "",
                    "max_consecutive_work_days": 5,
                    "night_shift_next_day_off": True,
                }
            )

    def save(self, shift_plan):
        shift_rule = self.shift_rule
        if shift_rule is None:
            try:
                shift_rule = shift_plan.shift_rule
            except ShiftRule.DoesNotExist:
                shift_rule = ShiftRule(
                    shift_plan=shift_plan,
                    max_consecutive_work_days=5,
                    night_shift_next_day_off=True,
                )
        shift_rule.required_day_staff = self.cleaned_data["required_day_staff"]
        shift_rule.required_night_staff = self.cleaned_data["required_night_staff"]
        shift_rule.off_days_per_staff = self.cleaned_data["off_days_per_staff"]
        shift_rule.required_leader_staff = self.cleaned_data["required_leader_staff"] or 0
        shift_rule.max_consecutive_work_days = self.cleaned_data["max_consecutive_work_days"]
        shift_rule.night_shift_next_day_off = self.cleaned_data["night_shift_next_day_off"]
        shift_rule.save()
        self.shift_rule = shift_rule
        return shift_rule


class WeekdayShiftRuleForm(forms.Form):
    selected = forms.CharField(required=False, widget=forms.HiddenInput)
    day_of_week = forms.IntegerField(min_value=0, max_value=6, widget=forms.HiddenInput)
    required_day_staff = forms.IntegerField(label="必要日勤数", min_value=0, required=False)
    required_night_staff = forms.IntegerField(label="必要夜勤数", min_value=0, required=False)
    required_leader_staff = forms.IntegerField(label="必要リーダー数", min_value=0, required=False)
    min_ability_level = forms.TypedChoiceField(
        label="勤務レベル",
        required=False,
        coerce=int,
        empty_value=None,
        choices=(("", "レベルを選択してください"), *StaffMember.AbilityLevelChoices.choices),
    )
    min_ability_level_staff_count = forms.IntegerField(
        label="必要人数",
        min_value=1,
        required=False,
    )
    memo = forms.CharField(
        label="メモ",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, day_of_week=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance

        for field_name, field in self.fields.items():
            if field_name == "selected":
                continue

            if field_name == "day_of_week":
                field.widget.attrs.update({"value": day_of_week if day_of_week is not None else ""})
                continue

            if field_name == "min_ability_level":
                field.widget.attrs.update(
                    {
                        "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
                    }
                )
                continue

            if field_name == "memo":
                field.widget.attrs.update(
                    {
                        "class": "textarea textarea-bordered w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none",
                        "placeholder": "必要なメモがあれば入力",
                    }
                )
                continue

            field.widget.attrs.update(
                {
                    "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
                }
            )

        if not self.is_bound:
            initial_day_of_week = instance.day_of_week if instance else day_of_week
            self.initial["selected"] = "1" if instance else "0"
            self.initial["day_of_week"] = initial_day_of_week
            if instance:
                self.initial.update(
                    {
                        "required_day_staff": instance.required_day_staff,
                        "required_night_staff": instance.required_night_staff,
                        "required_leader_staff": instance.required_leader_staff,
                        "min_ability_level": instance.min_ability_level,
                        "min_ability_level_staff_count": instance.min_ability_level_staff_count,
                        "memo": instance.memo,
                    }
                )

    def is_selected(self):
        if not self.is_bound:
            return self.initial.get("selected") == "1"
        return self.data.get(self.add_prefix("selected"), "0") == "1"

    def has_meaningful_input(self):
        if not self.is_bound:
            return bool(self.instance)

        if not self.is_selected():
            return False

        raw_fields = [
            self.data.get(self.add_prefix("required_day_staff"), ""),
            self.data.get(self.add_prefix("required_night_staff"), ""),
            self.data.get(self.add_prefix("required_leader_staff"), ""),
            self.data.get(self.add_prefix("min_ability_level"), ""),
            self.data.get(self.add_prefix("min_ability_level_staff_count"), ""),
            self.data.get(self.add_prefix("memo"), ""),
        ]
        return any(str(value).strip() for value in raw_fields)

    def clean(self):
        cleaned_data = super().clean()
        day_of_week = cleaned_data.get("day_of_week")
        if not self.is_selected():
            return cleaned_data

        has_meaningful_input = self.has_meaningful_input()
        min_ability_level = cleaned_data.get("min_ability_level")
        min_ability_level_staff_count = cleaned_data.get("min_ability_level_staff_count")

        if has_meaningful_input and day_of_week is None:
            self.add_error("day_of_week", "条件を設定する曜日を選択してください。")

        if (min_ability_level is None) ^ (min_ability_level_staff_count is None):
            message = "勤務レベル条件を使う場合は、レベルと人数を両方入力してください。"
            if min_ability_level is None:
                self.add_error("min_ability_level", message)
            if min_ability_level_staff_count is None:
                self.add_error("min_ability_level_staff_count", message)

        if day_of_week is None:
            return cleaned_data

        existing_rule = WeekdayShiftRule.objects.filter(
            shift_plan=self.instance.shift_plan if self.instance else None,
            day_of_week=day_of_week,
        )
        if self.instance:
            existing_rule = existing_rule.exclude(pk=self.instance.pk)
        if self.instance and existing_rule.exists():
            self.add_error("day_of_week", "選択した曜日の条件はすでに登録されています。")

        return cleaned_data

    def save(self, shift_plan):
        if not self.is_selected():
            if self.instance:
                self.instance.delete()
                self.instance = None
            return None

        if not self.has_meaningful_input():
            if self.instance:
                self.instance.delete()
                self.instance = None
            return None

        weekday_rule = self.instance or WeekdayShiftRule(shift_plan=shift_plan)
        weekday_rule.day_of_week = self.cleaned_data["day_of_week"]
        weekday_rule.required_day_staff = self.cleaned_data["required_day_staff"]
        weekday_rule.required_night_staff = self.cleaned_data["required_night_staff"]
        weekday_rule.required_leader_staff = self.cleaned_data["required_leader_staff"]
        weekday_rule.min_ability_level = self.cleaned_data["min_ability_level"]
        weekday_rule.min_ability_level_staff_count = self.cleaned_data["min_ability_level_staff_count"]
        weekday_rule.memo = self.cleaned_data["memo"].strip()
        weekday_rule.save()
        self.instance = weekday_rule
        return weekday_rule


class DateShiftRuleForm(forms.Form):
    active = forms.CharField(required=False, widget=forms.HiddenInput)
    date_rule_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    target_date = forms.DateField(
        label="対象日",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    required_day_staff = forms.IntegerField(label="必要日勤数", min_value=0, required=False)
    required_night_staff = forms.IntegerField(label="必要夜勤数", min_value=0, required=False)
    required_leader_staff = forms.IntegerField(label="必要リーダー数", min_value=0, required=False)
    min_ability_level = forms.TypedChoiceField(
        label="勤務レベル",
        required=False,
        coerce=int,
        empty_value=None,
        choices=(("", "レベルを選択してください"), *StaffMember.AbilityLevelChoices.choices),
    )
    min_ability_level_staff_count = forms.IntegerField(
        label="必要人数",
        min_value=1,
        required=False,
    )
    memo = forms.CharField(
        label="メモ",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, shift_plan, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_plan = shift_plan
        self.instance = instance

        self.fields["active"].widget.attrs.update({"value": "1" if instance else "0"})
        min_date, max_date = get_month_date_range(shift_plan.year, shift_plan.month)
        self.fields["target_date"].widget.attrs.update(
            {
                "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
                "min": min_date,
                "max": max_date,
            }
        )
        for field_name in ("required_day_staff", "required_night_staff", "required_leader_staff", "min_ability_level_staff_count"):
            self.fields[field_name].widget.attrs.update(
                {
                    "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
                }
            )
        self.fields["min_ability_level"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["memo"].widget.attrs.update(
            {
                "class": "textarea textarea-bordered w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none",
                "placeholder": "必要なメモがあれば入力",
            }
        )

        if not self.is_bound and instance:
            self.initial.update(
                {
                    "active": "1",
                    "date_rule_id": instance.pk,
                    "target_date": instance.target_date,
                    "required_day_staff": instance.required_day_staff,
                    "required_night_staff": instance.required_night_staff,
                    "required_leader_staff": instance.required_leader_staff,
                    "min_ability_level": instance.min_ability_level,
                    "min_ability_level_staff_count": instance.min_ability_level_staff_count,
                    "memo": instance.memo,
                }
            )

    def is_active(self):
        if not self.is_bound:
            return self.initial.get("active") == "1"
        return self.data.get(self.add_prefix("active"), "0") == "1"

    def has_meaningful_input(self):
        if not self.is_bound:
            return bool(self.instance)

        if not self.is_active():
            return False

        raw_fields = [
            self.data.get(self.add_prefix("target_date"), ""),
            self.data.get(self.add_prefix("required_day_staff"), ""),
            self.data.get(self.add_prefix("required_night_staff"), ""),
            self.data.get(self.add_prefix("required_leader_staff"), ""),
            self.data.get(self.add_prefix("min_ability_level"), ""),
            self.data.get(self.add_prefix("min_ability_level_staff_count"), ""),
            self.data.get(self.add_prefix("memo"), ""),
        ]
        return any(str(value).strip() for value in raw_fields)

    def clean(self):
        cleaned_data = super().clean()
        if not self.is_active():
            return cleaned_data

        if not self.has_meaningful_input():
            return cleaned_data

        target_date = cleaned_data.get("target_date")
        if target_date is None:
            self.add_error("target_date", "対象日を入力してください。")
            return cleaned_data

        if target_date.year != self.shift_plan.year or target_date.month != self.shift_plan.month:
            self.add_error("target_date", "対象シフト表の年月内の日付を選択してください。")

        existing_rule = self.shift_plan.date_rules.filter(target_date=target_date)
        if self.instance:
            existing_rule = existing_rule.exclude(pk=self.instance.pk)
        if existing_rule.exists():
            self.add_error("target_date", "同じ日付の特定日条件はすでに登録されています。")

        min_ability_level = cleaned_data.get("min_ability_level")
        min_ability_level_staff_count = cleaned_data.get("min_ability_level_staff_count")
        if (min_ability_level is None) ^ (min_ability_level_staff_count is None):
            message = "勤務レベル条件を使う場合は、レベルと人数を両方入力してください。"
            if min_ability_level is None:
                self.add_error("min_ability_level", message)
            if min_ability_level_staff_count is None:
                self.add_error("min_ability_level_staff_count", message)

        return cleaned_data

    def save(self, shift_plan):
        if not self.is_active():
            if self.instance:
                self.instance.delete()
                self.instance = None
            return None

        if not self.has_meaningful_input():
            return self.instance

        date_rule = self.instance or DateShiftRule(shift_plan=shift_plan)
        date_rule.target_date = self.cleaned_data["target_date"]
        date_rule.required_day_staff = self.cleaned_data["required_day_staff"]
        date_rule.required_night_staff = self.cleaned_data["required_night_staff"]
        date_rule.required_leader_staff = self.cleaned_data["required_leader_staff"]
        date_rule.min_ability_level = self.cleaned_data["min_ability_level"]
        date_rule.min_ability_level_staff_count = self.cleaned_data["min_ability_level_staff_count"]
        date_rule.memo = self.cleaned_data["memo"].strip()
        date_rule.save()
        self.instance = date_rule
        return date_rule
