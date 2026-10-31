"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:accounts:<name>``."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("auth/pin/", views.PinLoginView.as_view(), name="auth-pin"),
    path("auth/logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("me/", views.MeView.as_view(), name="me"),
]
