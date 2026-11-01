"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:office:<name>``."""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "office"

urlpatterns = [
    path("office/me/", views.MeView.as_view(), name="me"),
    path("office/today/", views.TodayView.as_view(), name="today"),
    path("office/approvals/", views.ApprovalsView.as_view(), name="approvals"),
    path("office/sites/<int:site_id>/days/<isodate:date>/", views.DayView.as_view(), name="day"),
    path(
        "office/sites/<int:site_id>/cash-sheets/<isodate:date>/",
        views.CashSheetView.as_view(),
        name="cash-sheet",
    ),
    path("office/workers/", views.WorkerListView.as_view(), name="workers"),
    path("office/workers/<int:worker_id>/", views.WorkerDetailView.as_view(), name="worker"),
    path(
        "office/days/<int:day_id>/decision-preview/",
        views.DecisionPreviewView.as_view(),
        name="decision-preview",
    ),
    path("office/decisions/", views.DecisionConfirmView.as_view(), name="decisions"),
]
