from django.urls import path

from . import views

app_name = "shifts"
urlpatterns = [
    path("", views.grid, name="grid"),
    path("api/set/", views.api_set, name="api_set"),
    path("copy-previous/", views.copy_previous, name="copy_previous"),
    path("share/", views.share, name="share"),
    path("image.png", views.image_png, name="image"),
    path("history/", views.history, name="history"),
    path("import/", views.import_view, name="import"),
]
