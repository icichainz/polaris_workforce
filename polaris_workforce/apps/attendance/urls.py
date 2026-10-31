from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("approvals/", views.approvals, name="approvals"),
]
