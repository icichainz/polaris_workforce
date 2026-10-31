from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("settings/audit-log/", views.audit_log_view, name="audit_log"),
]
