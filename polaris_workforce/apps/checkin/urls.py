"""Office pages under ``/checkin/``. Names: ``checkin:<name>``."""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "checkin"

urlpatterns = [
    path("checkin/", views.home, name="home"),
    path("checkin/sites/<int:pk>/<isodate:date>/", views.site_day, name="site_day"),
    path("checkin/photos/<int:pk>/", views.photo, name="photo"),
    path("checkin/badges/", views.badges, name="badges"),
    path("checkin/badges/print/", views.badge_sheet, name="badge_sheet"),
    path(
        "checkin/sites/<int:pk>/badges/issue/",
        views.badges_issue_missing,
        name="badges_issue_missing",
    ),
    path("checkin/workers/<int:pk>/badge/", views.badge_issue, name="badge_issue"),
    path("checkin/workers/<int:pk>/badge/revoke/", views.badge_revoke, name="badge_revoke"),
    path("checkin/kiosks/", views.kiosks, name="kiosks"),
    path("checkin/kiosks/<int:pk>/", views.kiosk_set, name="kiosk_set"),
    path("checkin/kiosks/<int:pk>/end/", views.kiosk_end, name="kiosk_end"),
    path("checkin/geofences/", views.geofences, name="geofences"),
    path("checkin/sites/<int:pk>/geofence/", views.geofence, name="geofence"),
]
