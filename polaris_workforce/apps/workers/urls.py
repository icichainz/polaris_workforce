from django.urls import path

from . import views

app_name = "workers"

urlpatterns = [
    path("workers/", views.worker_list, name="list"),
    path("workers/new/", views.worker_create, name="create"),
    path("workers/<int:pk>/", views.worker_detail, name="detail"),
    path("workers/<int:pk>/edit/", views.worker_edit, name="edit"),
]
