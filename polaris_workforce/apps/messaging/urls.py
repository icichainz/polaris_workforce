from django.urls import path

from . import views

app_name = "messaging"

urlpatterns = [
    path("messages/", views.messages_view, name="messages"),
    path("messages/settings/", views.settings_view, name="settings"),
    path("messages/recipients/", views.recipients_view, name="recipients"),
    path("messages/workers/<int:pk>/", views.worker_view, name="worker"),
]
