from django.urls import path

from . import views

app_name = "payroll"
urlpatterns = [
    path("", views.month_home, name="index"),
    path("list/", views.index, name="list"),
    path("action/", views.month_action, name="month_action"),
    path("sales/", views.sales, name="sales"),
    path("counts/", views.counts_import, name="counts_import"),
    path("expenses/approve-all/", views.expenses_approve_all, name="expenses_approve_all"),
    path("expenses/receipts/", views.receipts_upload, name="receipts_upload"),
    path("expenses/fix/", views.expenses_fix, name="expenses_fix"),
    path("review/", views.review, name="review"),
    path("transfers/", views.transfers, name="transfers"),
    path("transfers/toggle/", views.transfer_toggle, name="transfer_toggle"),
    path("review/<int:pk>/", views.review_action, name="review_action"),
    path("evidence/<int:pk>/", views.evidence_file, name="evidence"),
    path("employees/", views.employees, name="employees"),
    path("staff/", views.staff, name="staff"),
    path("teams/", views.teams, name="teams"),
    path("settings/", views.payroll_settings, name="settings"),
    path("settings/app-fee/", views.app_fee_settings, name="app_fee"),
    path("settings/transport/", views.transport_settings, name="transport_settings"),
    path("pdf/", views.month_pdf, name="month_pdf"),
    path("export.<str:fmt>", views.export, name="export"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/action/", views.detail_action, name="detail_action"),
    path("<int:pk>/pdf/", views.statement_pdf, name="statement_pdf"),
]
