from django import forms
from django.utils import timezone

from .models import ShiftPlan


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
