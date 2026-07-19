from django.contrib.auth import login, logout
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import FormView

from .forms import LoginForm, SignUpForm


class RedirectAuthenticatedUserMixin:
    """認証済みユーザーを公開ページからメイン画面へ戻すMixin。"""

    authenticated_redirect_url = reverse_lazy("core:main_page")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.authenticated_redirect_url)
        return super().dispatch(request, *args, **kwargs)


class SignUpView(RedirectAuthenticatedUserMixin, FormView):
    template_name = "accounts/signup.html"
    form_class = SignUpForm
    success_url = reverse_lazy("core:main_page")

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)
        return super().form_valid(form)


class LoginView(RedirectAuthenticatedUserMixin, FormView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    success_url = reverse_lazy("core:main_page")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        login(self.request, form.get_user())
        return super().form_valid(form)


class LogoutView(View):
    redirect_url = reverse_lazy("core:top_page")

    def post(self, request, *args, **kwargs):
        logout(request)
        return redirect(self.redirect_url)

    def get(self, request, *args, **kwargs):
        logout(request)
        return HttpResponseRedirect(self.redirect_url)
