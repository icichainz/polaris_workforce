from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("approvals/", views.approvals, name="approvals"),
    path("approvals/<int:day_id>/decide/", views.decide, name="decide"),
    path("approvals/bulk-approve/", views.bulk_approve, name="bulk_approve"),
]
