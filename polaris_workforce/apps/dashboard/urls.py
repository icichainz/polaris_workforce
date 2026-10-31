from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("reports/", views.reports, name="reports"),
    path("_components/", views.components, name="components"),
]
