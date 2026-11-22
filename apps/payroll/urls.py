from django.urls import path

from . import views

app_name = "payroll"
urlpatterns = [
    path("", views.month_home, name="index"),
    path("list/", views.index, name="list"),
    path("action/", views.month_action, name="month_action"),
    path("sales/", views.sales, name="sales"),
    path("counts/", views.counts_import, name="counts_import"),
    path("evidence/<int:pk>/", views.evidence_file, name="evidence"),
    path("employees/", views.employees, name="employees"),
    path("teams/", views.teams, name="teams"),
    path("pdf/", views.month_pdf, name="month_pdf"),
    path("export.<str:fmt>", views.export, name="export"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/action/", views.detail_action, name="detail_action"),
    path("<int:pk>/pdf/", views.statement_pdf, name="statement_pdf"),
]
