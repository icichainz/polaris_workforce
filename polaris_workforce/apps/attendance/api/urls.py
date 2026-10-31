"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:attendance:<name>``."""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "attendance"

urlpatterns = [
    path("sites/<int:site_id>/days/", views.DayListView.as_view(), name="days"),
    path("sites/<int:site_id>/days/<isodate:date>/", views.DayDetailView.as_view(), name="day"),
    path(
        "sites/<int:site_id>/days/<isodate:date>/records/",
        views.RecordBatchView.as_view(),
        name="day-records",
    ),
    path(
        "sites/<int:site_id>/days/<isodate:date>/submit/",
        views.SubmitDayView.as_view(),
        name="day-submit",
    ),
    path(
        "records/<int:record_id>/corrections/",
        views.CorrectionCreateView.as_view(),
        name="record-corrections",
    ),
    path("sync/", views.SyncView.as_view(), name="sync"),
]
