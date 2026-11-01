"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:presence:<name>``."""

from django.urls import path

from . import views

app_name = "presence"

urlpatterns = [
    path("tracking/token/", views.TrackingTokenView.as_view(), name="tracking-token"),
    path("tracking/consent/", views.TrackingConsentView.as_view(), name="tracking-consent"),
    path("presence/events/", views.PresenceEventsView.as_view(), name="events"),
    path(
        "sites/<int:site_id>/presence-sessions/",
        views.SiteSessionsView.as_view(),
        name="site-sessions",
    ),
]
