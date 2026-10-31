from django.urls import path

from . import views

app_name = "payroll"

urlpatterns = [
    path("cash-sheets/", views.cash_sheets, name="cash_sheets"),
    path("pay-periods/", views.periods, name="periods"),
    path("advances/", views.advances, name="advances"),
]
