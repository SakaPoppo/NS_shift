from django.urls import path

from .views import (
    ShiftPlanCreateView,
    ShiftPlanCsvExportView,
    ShiftPlanDeleteView,
    ShiftPlanEditView,
    ShiftPlanListView,
    ShiftRuleEditView,
)

app_name = "shifts"

# shifts アプリ内で使うURL一覧。
# 受け取るもの: ブラウザからのパスと必要に応じた pk
# 返すもの: 対応する view へのルーティング設定
urlpatterns = [
    path("", ShiftPlanListView.as_view(), name="list"),
    path("create/", ShiftPlanCreateView.as_view(), name="create"),
    path("<int:pk>/conditions/", ShiftRuleEditView.as_view(), name="conditions"),
    path("<int:pk>/edit/", ShiftPlanEditView.as_view(), name="edit"),
    path("<int:pk>/delete/", ShiftPlanDeleteView.as_view(), name="delete"),
    path("<int:pk>/export/csv/", ShiftPlanCsvExportView.as_view(), name="export_csv"),
]
