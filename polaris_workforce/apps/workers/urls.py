from django.urls import path

from . import views

app_name = "workers"

urlpatterns = [
    path("workers/", views.worker_list, name="list"),
    path("workers/new/", views.worker_create, name="create"),
    path("workers/import/", views.worker_import, name="import"),
    path("workers/import/confirm/", views.worker_import_confirm, name="import_confirm"),
    path("workers/import/template.xlsx", views.worker_import_template, name="import_template"),
    path("workers/<int:pk>/", views.worker_detail, name="detail"),
    path("workers/<int:pk>/edit/", views.worker_edit, name="edit"),
    path("workers/<int:pk>/documents/", views.document_upload, name="document_upload"),
    path(
        "workers/<int:pk>/documents/<int:document_id>/delete/",
        views.document_delete,
        name="document_delete",
    ),
    path("workers/documents/<int:document_id>/file/", views.document_file, name="document_file"),
]
