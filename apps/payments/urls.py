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
    path("monthly/", views.add_monthly, name="add_monthly"),
    path("import-sheet/", views.import_sheet, name="import_sheet"),
]
