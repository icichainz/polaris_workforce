from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("settings/audit-log/", views.audit_log_view, name="audit_log"),
    # Uploaded files, never served from MEDIA_ROOT directly (see apps.core.files).
    path("files/workers/<int:pk>/photo/", views.worker_photo, name="worker_photo"),
    path("files/proofs/<int:pk>/", views.payment_proof, name="payment_proof"),
    path("files/media/<path:name>", views.media_file, name="media_file"),
]
