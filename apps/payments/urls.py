from django.urls import path

from . import views

app_name = "payments"
urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("new/", views.edit, name="new"),
    path("<int:pk>/", views.edit, name="edit"),
    path("<int:pk>/paid/", views.toggle_paid, name="toggle_paid"),
    path("<int:pk>/file/", views.invoice_file, name="file"),
    path("<int:pk>/reread/", views.reread, name="reread"),
    path("reread-all/", views.reread_all, name="reread_all"),
    path("monthly/", views.add_monthly, name="add_monthly"),
    path("import-sheet/", views.import_sheet, name="import_sheet"),
    path("sales/", views.sales, name="sales"),
    path("sales/new/", views.sale_edit, name="sale_new"),
    path("sales/upload/", views.sale_upload, name="sale_upload"),
    path("sales/<int:pk>/file/", views.sale_file, name="sale_file"),
    path("sales/<int:pk>/", views.sale_edit, name="sale_edit"),
    path("sales/<int:pk>/received/", views.sale_toggle, name="sale_toggle"),
    path("year/", views.yearly, name="yearly"),
]
