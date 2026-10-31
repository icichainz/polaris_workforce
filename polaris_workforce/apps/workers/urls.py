from django.urls import path

from . import views

app_name = "workers"

urlpatterns = [
    path("workers/", views.worker_list, name="list"),
]
