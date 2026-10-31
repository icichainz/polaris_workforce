from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("reports/", views.reports, name="reports"),
    path("reports/attendance.csv", views.reports_csv, name="reports_csv"),
    path("_components/", views.components, name="components"),
]
