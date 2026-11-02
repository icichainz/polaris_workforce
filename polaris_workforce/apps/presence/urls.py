"""Office pages under ``/presence/``. Names: ``presence:<name>``.

The phone and tracker endpoints are in ``apps/presence/api/urls.py`` (under ``/api/v1/``).
"""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "presence"

urlpatterns = [
    path("presence/", views.home, name="home"),
    path("presence/token/", views.dashboard_token, name="dashboard_token"),
    path("presence/notice/", views.notice, name="notice"),
    path("presence/sites/<int:pk>/", views.site_setup, name="site_setup"),
    path("presence/sites/<int:pk>/live/", views.live, name="live"),
    path("presence/sites/<int:pk>/push/", views.site_push, name="site_push"),
    path("presence/sites/<int:pk>/zones/new/", views.zone_create, name="zone_create"),
    path("presence/zones/<int:pk>/", views.zone_edit, name="zone_edit"),
    path("presence/zones/<int:pk>/delete/", views.zone_delete, name="zone_delete"),
    path("presence/sites/<int:pk>/gateways/new/", views.gateway_create, name="gateway_create"),
    path("presence/gateways/<int:pk>/", views.gateway_edit, name="gateway_edit"),
    path("presence/gateways/<int:pk>/token/", views.gateway_token, name="gateway_token"),
    path("presence/gateways/<int:pk>/revoke/", views.gateway_revoke, name="gateway_revoke"),
    path("presence/badges/", views.badges, name="badges"),
    path("presence/badges/<int:pk>/end/", views.badge_end, name="badge_end"),
    path("presence/workers/<int:pk>/badge/", views.worker_badge, name="worker_badge"),
    path("presence/workers/<int:pk>/export/", views.worker_export, name="worker_export"),
    path("presence/people/<str:kind>/<int:pk>/", views.person_day, name="person"),
    path(
        "presence/people/<str:kind>/<int:pk>/<isodate:date>/",
        views.person_day,
        name="person_day",
    ),
]
