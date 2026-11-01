from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    path("settings/ai-assistants/", views.access, name="access"),
    path("settings/ai-assistants/new/", views.create, name="create"),
    path("settings/ai-assistants/<int:pk>/revoke/", views.revoke, name="revoke"),
]
