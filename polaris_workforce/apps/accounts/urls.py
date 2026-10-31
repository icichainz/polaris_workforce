from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("users/", views.users, name="users"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/pin/", views.user_set_pin, name="user_set_pin"),
    path(
        "users/<int:pk>/devices/<int:device_pk>/revoke/",
        views.device_revoke,
        name="device_revoke",
    ),
]
