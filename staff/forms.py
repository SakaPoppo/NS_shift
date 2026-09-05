from django import forms
from django.forms import BaseModelFormSet, modelformset_factory

from .constants import MAX_ACTIVE_STAFF_COUNT
from .models import StaffMember, StaffRegularDayOff


BULK_STAFF_LEVELS = range(5, 0, -1)
BULK_STAFF_LEVEL_LABELS = {
    5: "Lv5 管理者",
    4: "Lv4 重症対応",
    3: "Lv3 指導者",
    2: "Lv2 自立",
    1: "Lv1 新人",
}


class BulkStaffSetupForm(forms.Form):
    """一括登録するスタッフのLv別内訳を受け付けるフォーム。"""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for level in BULK_STAFF_LEVELS:
            self.fields[f"level_{level}_count"] = forms.IntegerField(
                label=f"Lv{level}の人数",
                min_value=0,
                initial=0,
                widget=forms.NumberInput(
                    attrs={
                        "class": "hidden",
                        "data-staff-count-input": "true",
                    }
                ),
            )
            self.fields[f"level_{level}_all_leaders"] = forms.BooleanField(
                label=f"Lv{level}を全員リーダーにする",
                required=False,
                widget=forms.CheckboxInput(
                    attrs={"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
                ),
            )
            self.fields[f"level_{level}_use_leader_count"] = forms.BooleanField(
                label=f"Lv{level}で指定数だけリーダーにする",
                required=False,
                widget=forms.CheckboxInput(
                    attrs={"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
                ),
            )
            self.fields[f"level_{level}_leader_count"] = forms.IntegerField(
                label=f"Lv{level}のリーダー数",
                min_value=0,
                required=False,
                initial=0,
                widget=forms.NumberInput(
                    attrs={
                        "class": "hidden",
                        "data-leader-count-input": "true",
                    }
                ),
            )

    @property
    def level_rows(self):
        """テンプレートが動的なフィールド名を組み立てずに済む表示用データ。"""
        return [
            {
                "level": level,
                "label": BULK_STAFF_LEVEL_LABELS[level],
                "short_label": BULK_STAFF_LEVEL_LABELS[level].split(" ", 1)[1],
                "count": self[f"level_{level}_count"],
                "all_leaders": self[f"level_{level}_all_leaders"],
                "use_leader_count": self[f"level_{level}_use_leader_count"],
                "leader_count": self[f"level_{level}_leader_count"],
            }
            for level in BULK_STAFF_LEVELS
        ]

    def clean(self):
        cleaned_data = super().clean()
        total_count = 0

        for level in BULK_STAFF_LEVELS:
            count = cleaned_data.get(f"level_{level}_count")
            all_leaders = cleaned_data.get(f"level_{level}_all_leaders")
            use_leader_count = cleaned_data.get(f"level_{level}_use_leader_count")
            leader_count = cleaned_data.get(f"level_{level}_leader_count") or 0

            if count is not None:
                total_count += count
            if all_leaders and use_leader_count:
                message = f"Lv{level}は全員リーダーと指定数だけリーダーを同時に選択できません。"
                self.add_error(f"level_{level}_all_leaders", message)
                self.add_error(f"level_{level}_use_leader_count", message)
            if count is not None and leader_count > count:
                self.add_error(
                    f"level_{level}_leader_count",
                    f"Lv{level}の指定リーダー数は人数以下にしてください。",
                )

        if total_count == 0:
            raise forms.ValidationError("スタッフ数の合計は1人以上にしてください。")

        if self.user and (
            StaffMember.objects.filter(user=self.user, is_active=True).count() + total_count
            > MAX_ACTIVE_STAFF_COUNT
        ):
            raise forms.ValidationError(
                f"現在の在籍スタッフ数と今回の登録人数の合計は{MAX_ACTIVE_STAFF_COUNT}人以下にしてください。"
            )

        return cleaned_data

    def build_member_initial_data(self):
        """入力されたLv別の人数から、確認用フォームの初期値を生成する。"""
        if not self.is_valid():
            raise ValueError("有効な一括登録設定フォームからのみ初期値を生成できます。")

        initial_data = []
        for level in BULK_STAFF_LEVELS:
            count = self.cleaned_data[f"level_{level}_count"]
            if self.cleaned_data[f"level_{level}_all_leaders"]:
                leader_count = count
            elif self.cleaned_data[f"level_{level}_use_leader_count"]:
                leader_count = self.cleaned_data[f"level_{level}_leader_count"] or 0
            else:
                leader_count = 0

            for index in range(count):
                initial_data.append(
                    {
                        "name": "No name",
                        "gender": StaffMember.GenderChoices.FEMALE,
                        "job": StaffMember.JobChoices.NURSE,
                        "role": (
                            StaffMember.RoleChoices.LEADER
                            if index < leader_count
                            else StaffMember.RoleChoices.MEMBER
                        ),
                        "ability_level": level,
                        "can_night_shift": True,
                        "regular_days_off": [],
                        "is_holiday_off": False,
                    }
                )

        return initial_data


class StaffMemberForm(forms.ModelForm):
    """スタッフの基本情報と曜日固定休をまとめて扱うフォーム。

    regular_days_off は StaffMember の直接フィールドではなく、
    関連モデルの StaffRegularDayOff を更新するための入力欄として扱う。
    """

    gender = forms.ChoiceField(
        label="性別",
        choices=(
            (StaffMember.GenderChoices.FEMALE, "女性"),
            (StaffMember.GenderChoices.MALE, "男性"),
        ),
        required=True,
        widget=forms.RadioSelect,
        initial=StaffMember.GenderChoices.FEMALE,
    )
    can_night_shift = forms.TypedChoiceField(
        label="夜勤の可否",
        choices=((True, "可"), (False, "不可")),
        coerce=lambda value: value in {True, "True", "true", "1", "on"},
        required=True,
        widget=forms.RadioSelect,
        initial=True,
    )
    regular_days_off = forms.TypedMultipleChoiceField(
        label="固定休",
        choices=StaffRegularDayOff.DayOfWeekChoices.choices,
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = StaffMember
        fields = (
            "name", "gender", "job", "role", "ability_level",
            "can_night_shift", "is_holiday_off",
        )
        widgets = {
            "gender": forms.RadioSelect,
            "job": forms.Select,
            "role": forms.Select,
            "ability_level": forms.Select,
            "is_holiday_off": forms.CheckboxInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("job", "role", "ability_level", "can_night_shift"):
            self.fields[field_name].required = True
        self.fields["gender"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["can_night_shift"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["regular_days_off"].widget.attrs.update({"class": "checkbox checkbox-primary checkbox-sm rounded-md"})
        self.fields["is_holiday_off"].widget.attrs.update(
            {"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
        )
        self.fields["name"].widget.attrs.update(
            {
                "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none",
                "placeholder": "氏名を入力",
            }
        )
        self.fields["name"].help_text = "例：山田 花子"
        self.fields["job"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["role"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["ability_level"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["ability_level"].help_text = (
            "1は自立前、3は新人指導可能、5は管理代行業務まで担える目安です。"
        )
        if self.instance and self.instance.pk:
            # 編集画面では、関連テーブルに保存済みの固定休をチェックボックスへ戻す。
            self.fields["regular_days_off"].initial = list(
                self.instance.regular_days_off.values_list("day_of_week", flat=True)
            )


StaffMemberCreateForm = StaffMemberForm


class BulkStaffMemberForm(StaffMemberForm):
    """一括登録・一括編集で再利用するスタッフ1人分のフォーム。"""

    can_night_shift = forms.TypedChoiceField(
        label="夜勤の可否",
        choices=((True, "可"), (False, "不可")),
        coerce=lambda value: value in {True, "True", "true", "1", "on"},
        required=True,
        initial=True,
        widget=forms.Select,
    )

    class Meta(StaffMemberForm.Meta):
        fields = (
            "name",
            "gender",
            "job",
            "role",
            "ability_level",
            "can_night_shift",
            "is_holiday_off",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        compact_select_class = (
            "select select-bordered h-10 min-h-10 w-full rounded-lg border-slate-300 "
            "bg-white px-2 text-xs font-semibold text-slate-700 focus:border-sky-600 focus:outline-none"
        )
        self.fields["name"].widget.attrs.update(
            {
                "class": "input input-bordered h-10 w-full rounded-lg border-slate-300 bg-white px-3 text-sm font-semibold text-slate-700 focus:border-sky-600 focus:outline-none",
                "placeholder": "氏名",
                "data-name-input": "true",
            }
        )
        self.fields["name"].help_text = ""
        for field_name in ("gender", "job", "role", "ability_level", "can_night_shift"):
            field = self.fields[field_name]
            field.widget = forms.Select(choices=field.choices, attrs={"class": compact_select_class})
            field.help_text = ""
        self.fields["gender"].widget.attrs["data-gender-select"] = "true"
        self.fields["role"].widget.attrs["data-role-select"] = "true"
        self.fields["ability_level"].choices = tuple(
            (level, f"Lv{level}") for level in BULK_STAFF_LEVELS
        )
        self.fields["ability_level"].widget.attrs["data-ability-level-select"] = "true"
        self.fields["can_night_shift"].widget.attrs["data-night-select"] = "true"
        self.fields["regular_days_off"].widget.attrs.update(
            {"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
        )
        self.fields["is_holiday_off"].widget.attrs.update(
            {"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
        )


class BulkStaffEditForm(BulkStaffMemberForm):
    """既存スタッフの一括編集だけで使用する削除指定付きフォーム。"""

    delete_staff = forms.BooleanField(
        label="削除",
        required=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": "checkbox checkbox-error checkbox-sm rounded-md text-white",
                "data-delete-checkbox": "true",
                "aria-label": "削除対象にする",
            }
        ),
    )


class BaseBulkStaffMemberFormSet(BaseModelFormSet):
    """フォーム1で決まった人数分だけ、未保存スタッフ用フォームを表示する。"""

    def __init__(self, *args, expected_form_count=0, **kwargs):
        self.expected_form_count = expected_form_count
        self.extra = expected_form_count
        super().__init__(*args, **kwargs)

    def _construct_form(self, index, **kwargs):
        form = super()._construct_form(index, **kwargs)
        # DB未保存のModelFormsetでは全行が extra form 扱いになるため、
        # フォーム1で生成した行は空欄でも無視されないよう必須検証を行う。
        if index < self.expected_form_count:
            form.empty_permitted = False
        return form

    def clean(self):
        super().clean()

        if self.total_form_count() != self.expected_form_count:
            raise forms.ValidationError(
                "登録対象のスタッフ数が不正です。最初からやり直してください。"
            )


BulkStaffMemberFormSet = modelformset_factory(
    StaffMember,
    form=BulkStaffMemberForm,
    formset=BaseBulkStaffMemberFormSet,
    extra=0,
    max_num=MAX_ACTIVE_STAFF_COUNT,
    validate_max=True,
)


class BaseBulkStaffEditFormSet(BaseModelFormSet):
    """既存スタッフだけを対象にする一括編集用FormSet。"""

    def __init__(
        self,
        *args,
        expected_form_count=0,
        expected_staff_ids=(),
        **kwargs,
    ):
        self.expected_form_count = expected_form_count
        self.expected_staff_ids = set(expected_staff_ids)
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()

        if (
            self.total_form_count() != self.expected_form_count
            or self.initial_form_count() != self.expected_form_count
        ):
            raise forms.ValidationError(
                "編集対象のスタッフ数が不正です。最初からやり直してください。"
            )

        if any(form.instance.pk not in self.expected_staff_ids for form in self.forms):
            raise forms.ValidationError(
                "編集対象のスタッフに不正なデータが含まれています。最初からやり直してください。"
            )


BulkStaffEditFormSet = modelformset_factory(
    StaffMember,
    form=BulkStaffEditForm,
    formset=BaseBulkStaffEditFormSet,
    extra=0,
)
