from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.accounts import views as account_views
from apps.core import setup_views
from apps.core import views as core_views

admin.site.site_header = "社内管理システム"
admin.site.site_title = "社内管理システム"

urlpatterns = [
    path("", core_views.dashboard, name="dashboard"),
    path("healthz", core_views.healthz, name="healthz"),
    path("login/", account_views.OptionalLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("stepup/<str:scope>/", account_views.stepup_view, name="stepup"),
    path("stepup/<str:scope>/close/", account_views.stepup_close_view, name="stepup_close"),
    path("settings/", setup_views.setup, name="setup"),
    path("settings/backup/", setup_views.backup, name="backup"),
    path("settings/stepup-passwords/", account_views.stepup_password_change_view, name="stepup_password_change"),
    path("shifts/", include("apps.shifts.urls")),
    path("attendance/", include("apps.attendance.urls")),
    path("members/", include("apps.members.urls")),
    path("payroll/", include("apps.payroll.urls")),
    path("expenses/", include("apps.expenses.urls")),
    path("payments/", include("apps.payments.urls")),
    # 給与の画面から経費を扱う入口（給与のパスワードで守る。中身は経費の画面と同じ）
    path("payroll/expenses/", include(("apps.expenses.urls", "expenses"), namespace="payroll_expenses")),
    path("admin/", admin.site.urls),
]
