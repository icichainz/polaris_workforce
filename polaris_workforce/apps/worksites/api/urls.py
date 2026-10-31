"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:worksites:<name>``."""

from django.urls import path

from . import views

app_name = "worksites"

urlpatterns = [
    path("sites/<int:site_id>/crew/", views.CrewView.as_view(), name="crew"),
]
