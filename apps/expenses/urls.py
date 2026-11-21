from django.urls import path

from . import views

app_name = "expenses"
urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("line/", views.line_import, name="line_import"),
    path("files/<int:pk>/", views.file_detail, name="file"),
    path("files/<int:pk>/action/", views.file_action, name="file_action"),
    path("files/<int:pk>/mapping/", views.mapping, name="mapping"),
    path("files/<int:pk>/download/", views.source_download, name="download"),
    path("files/<int:pk>/page/<int:page>.png", views.page_image, name="page"),
    path("records/<int:pk>/action/", views.record_action, name="record_action"),
    path("issues/<int:pk>/resolve/", views.file_issue_resolve, name="issue_resolve"),
]
