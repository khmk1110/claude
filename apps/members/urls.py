from django.urls import path

from . import views

app_name = "members"
urlpatterns = [
    path("", views.member_list, name="list"),
    path("order/", views.order, name="order"),
    path("new/", views.edit, name="new"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/delete/", views.delete, name="delete"),
    path("cards/", views.cards, name="cards"),
]
