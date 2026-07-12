from django import forms
from django.utils import timezone

from .models import ShiftPlan, ShiftRule


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
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
                "placeholder": "例：2026年7月 ICUシフト表",
            }
        )
        self.fields["year"].initial = self.initial.get("year", initial_year)
        self.fields["year"].widget.attrs.update(
            {
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
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
    )
    max_consecutive_work_days = forms.IntegerField(
        label="最大連勤数",
        min_value=1,
    )
    night_shift_next_day_off = forms.BooleanField(
        label="明け翌日を公休にする",
        required=False,
    )

    def __init__(self, *args, shift_rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_rule = shift_rule

        for name, field in self.fields.items():
            if name == "night_shift_next_day_off":
                field.widget.attrs.update(
                    {
                        "class": "h-5 w-5 rounded border-slate-300 text-sky-700 focus:ring-sky-700",
                    }
                )
                continue

            field.widget.attrs.update(
                {
                    "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
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
                    "required_leader_staff": 0,
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
        shift_rule.required_leader_staff = self.cleaned_data["required_leader_staff"]
        shift_rule.max_consecutive_work_days = self.cleaned_data["max_consecutive_work_days"]
        shift_rule.night_shift_next_day_off = self.cleaned_data["night_shift_next_day_off"]
        shift_rule.save()
        self.shift_rule = shift_rule
        return shift_rule
