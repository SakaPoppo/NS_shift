from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # 自動生成されるIDフィールドの型
    name = "accounts"  # アプリ名　INSTALLED_APPSに登録する際の名前
