from django.urls import path

from . import views

app_name = "attendance"
urlpatterns = [
    path("", views.month_view, name="month"),
    path("lock/", views.lock_toggle, name="lock"),
    path("leave/", views.leave_list, name="leave_list"),
    path("leave/<int:member_id>/", views.leave_member, name="leave_member"),
    path("leave/<int:member_id>/action/", views.leave_action, name="leave_action"),
    path("<int:member_id>/", views.member_view, name="member"),
    path("<int:member_id>/update/", views.member_update, name="member_update"),
]
