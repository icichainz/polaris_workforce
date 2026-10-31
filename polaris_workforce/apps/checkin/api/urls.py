"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:checkin:<name>``."""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "checkin"

urlpatterns = [
    path("checkins/", views.CheckInCreateView.as_view(), name="checkins"),
    path("checkin/device/", views.DeviceView.as_view(), name="device"),
    path("sites/<int:site_id>/checkin-setup/", views.SetupView.as_view(), name="setup"),
    path(
        "sites/<int:site_id>/checkins/<isodate:date>/",
        views.SiteCheckInsView.as_view(),
        name="site-checkins",
    ),
    path(
        "sites/<int:site_id>/days/<isodate:date>/suggestions/",
        views.SuggestionsView.as_view(),
        name="day-suggestions",
    ),
    path(
        "files/checkins/<int:checkin_id>/photo/",
        views.CheckInPhotoView.as_view(),
        name="file-photo",
    ),
]
