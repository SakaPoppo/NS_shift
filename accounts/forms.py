from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError


User = get_user_model()
REGISTRATION_UNAVAILABLE_MESSAGE = (
    "入力内容では登録できませんでした。内容を変更してもう一度お試しください。"
)


class SignUpForm(UserCreationForm):
    """ユーザー登録フォーム。

    UserCreationForm に含まれない email の必須入力と重複確認を追加する。
    """

    email = forms.EmailField(
        label="メールアドレス",
        max_length=254,
        required=True,
        error_messages={"required": "メールアドレスを入力してください。"},
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "password1", "password2")
        error_messages = {
            "username": {
                "unique": REGISTRATION_UNAVAILABLE_MESSAGE,
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "ユーザー名"}
        )
        self.fields["email"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "email@example.com"}
        )
        self.fields["password1"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "パスワード"}
        )
        self.fields["password2"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "パスワード（確認用）"}
        )

    def clean_email(self):
        # 大文字小文字だけ違うメールアドレスも同一とみなし、重複登録を防ぐ。
        email = self.cleaned_data["email"].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(REGISTRATION_UNAVAILABLE_MESSAGE)
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class LoginForm(forms.Form):
    """ユーザー名またはメールアドレスで認証するログインフォーム。"""

    username_or_email = forms.CharField(label="ユーザー名またはメールアドレス", max_length=254)
    password = forms.CharField(label="パスワード", strip=False, widget=forms.PasswordInput)
    error_messages = {
        "invalid_login": "ユーザー名またはメールアドレス、パスワードが正しくありません。",
    }

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)
        self.fields["username_or_email"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "ユーザー名またはメールアドレス"}
        )
        self.fields["password"].widget.attrs.update(
            {"class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none", "placeholder": "パスワード"}
        )

    def clean(self):
        cleaned_data = super().clean()
        identifier = cleaned_data.get("username_or_email")
        password = cleaned_data.get("password")

        if identifier and password:
            self.user_cache = self.authenticate_user(identifier, password)
            if self.user_cache is None:
                raise ValidationError(self.error_messages["invalid_login"])

        return cleaned_data

    def authenticate_user(self, identifier, password):
        """ユーザー名認証を試し、失敗した場合だけメールアドレスから再解決する。"""
        user = authenticate(self.request, username=identifier, password=password)
        if user is not None:
            return user

        matched_user = User.objects.filter(email__iexact=identifier).first()
        if matched_user is None:
            return None

        # Django の標準 authenticate は username ベースなので、メール一致時だけ username に戻す。
        return authenticate(self.request, username=matched_user.get_username(), password=password)

    def get_user(self):
        return self.user_cache
