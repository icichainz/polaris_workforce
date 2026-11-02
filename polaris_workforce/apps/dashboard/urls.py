from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("reports/", views.reports, name="reports"),
    path("reports/cash/", views.reports_cash, name="reports_cash"),
    path("reports/checks/", views.reports_checks, name="reports_checks"),
    path("reports/budget-warning/", views.budget_warning, name="budget_warning"),
    path("reports/attendance.csv", views.reports_csv, name="reports_csv"),
    path("reports/report.xlsx", views.reports_xlsx, name="reports_xlsx"),
    path("_components/", views.components, name="components"),
]
