from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError


User = get_user_model()


class SignUpForm(UserCreationForm):  # 新規登録フォーム
    email = forms.EmailField(label="メールアドレス", max_length=254)  # 標準のUserCreationFormにはemailフィールドがないため追加

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):  # フォームの初期化をした際の処理、CSSクラスを追加するなど見た目を調整
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "ユーザー名"}
        )
        self.fields["email"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "email@example.com"}
        )
        self.fields["password1"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "パスワード"}
        )
        self.fields["password2"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "パスワード（確認用）"}
        )

    def clean_email(self):  # フォームに入力されたemailを取り出す
        email = self.cleaned_data["email"].strip()  # 前後の空白を削除
        if User.objects.filter(email__iexact=email).exists():  # 既に登録されているメールアドレスか確認
            raise ValidationError("このメールアドレスはすでに登録されています。")
        return email  # 重複がなければ、そのemailを正式な検証済みデータとして返す

    def save(self, commit=True):  # ユーザーを保存、commit=TrueにしてるのはDBへ保存するため
        user = super().save(commit=False)  #ユーザーを保存する前に、commit=Falseで一時的にDBへ保存せずにuserオブジェクトを取得
        user.email = self.cleaned_data["email"]  # フォームから取得したメールアドレスをuserオブジェクトに設定
        if commit:  # ユーザーをDBへ保存　ここでみてるcommitはsaveメソッドの引数のこと
            user.save()
        return user


class LoginForm(forms.Form):  # DBに保存しないのでforms.Formを継承
    # フォームを作る時とバリデーションの時に使用
    username_or_email = forms.CharField(label="ユーザー名またはメールアドレス", max_length=254)
    password = forms.CharField(label="パスワード", strip=False, widget=forms.PasswordInput)
    # widgetでPasswordInputを指定、パスワード入力欄が伏せ字になる
    error_messages = {
        "invalid_login": "ユーザー名またはメールアドレス、パスワードが正しくありません。",
    }

    def __init__(self, request=None, *args, **kwargs):  # フォームの初期化時にリクエストを受け取る
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)
        self.fields["username_or_email"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "ユーザー名またはメールアドレス"}
        )
        self.fields["password"].widget.attrs.update(
            {"class": "input input-bordered w-full bg-white", "placeholder": "パスワード"}
        )

    def clean(self):  # ログインは組み合わせで認証するから、フォーム全体のcleanを使う
        cleaned_data = super().clean()
        identifier = cleaned_data.get("username_or_email")
        password = cleaned_data.get("password")

        if identifier and password:  # identifierとpasswordの両方が存在する場合
            self.user_cache = self.authenticate_user(identifier, password)
            if self.user_cache is None:
                raise ValidationError(self.error_messages["invalid_login"])

        return cleaned_data 

    def authenticate_user(self, identifier, password):  # ユーザー名なのかアドレスなのかを判定して認証
        user = authenticate(self.request, username=identifier, password=password)  # まずはusernameとして認証してみる
        if user is not None:  # usernameで認証成功
            return user

        matched_user = User.objects.filter(email__iexact=identifier).first()  # usernameじゃないならemailとして認証してみる
        if matched_user is None:  # emailが見つからなければ認証失敗
            return None

        return authenticate(self.request, username=matched_user.get_username(), password=password)  
    # Djangoのauthenticateはusernameでしか認証できないので、emailからusernameを取得して再度認証する

    def get_user(self):
        return self.user_cache
