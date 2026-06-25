from django.contrib.auth import login, logout
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import FormView

from .forms import LoginForm, SignUpForm


class RedirectAuthenticatedUserMixin:  # 認証済みユーザーはメインページへリダイレクトする
    authenticated_redirect_url = reverse_lazy("core:main_page")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:  # 認証済みユーザーか？
            return redirect(self.authenticated_redirect_url)
        return super().dispatch(request, *args, **kwargs)  #superはこのclassのすぐ右側にいるclassのdispatchを呼び出す


class SignUpView(RedirectAuthenticatedUserMixin, FormView):
    template_name = "accounts/signup.html"
    form_class = SignUpForm
    success_url = reverse_lazy("core:main_page")  # 登録成功後にリダイレクトするURL

    def form_valid(self, form):
        user = form.save()  # ユーザーをDBに保存
        login(self.request, user)  # 登録したユーザーをログイン
        return super().form_valid(form)  # FormViewのform_validへ渡してsuccess_urlへリダイレクトさせる


class LoginView(RedirectAuthenticatedUserMixin, FormView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    success_url = reverse_lazy("core:main_page")

    def get_form_kwargs(self):  # リクエスト情報をkwargsに追加してフォームに渡す
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):  # フォームが有効な場合、リクエスト内容を使ってユーザーをログインさせる
        login(self.request, form.get_user())
        return super().form_valid(form)


class LogoutView(View):
    redirect_url = reverse_lazy("core:top_page")  # ログアウト後にリダイレクトするURL

    def post(self, request, *args, **kwargs):  # POSTリクエストが来たらログアウト
        logout(request)
        return redirect(self.redirect_url)

    def get(self, request, *args, **kwargs):  # GETリクエストが来たらログアウト
        logout(request)
        return HttpResponseRedirect(self.redirect_url)
