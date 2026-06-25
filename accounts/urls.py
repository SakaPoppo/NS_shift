from django.urls import path

from .views import LoginView, LogoutView, SignUpView

app_name = "accounts"  # アプリ名　URLの名前空間 /accounts/〜の部分

urlpatterns = [
    path("signup/", SignUpView.as_view(), name="signup"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
