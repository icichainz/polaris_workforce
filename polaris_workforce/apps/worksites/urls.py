from django.urls import path

from . import views

app_name = "worksites"

urlpatterns = [
    path("sites/", views.site_list, name="list"),
    path("sites/new/", views.site_create, name="create"),
    path("sites/<int:pk>/", views.site_detail, name="detail"),
    path("sites/<int:pk>/edit/", views.site_edit, name="edit"),
    path("sites/<int:pk>/crew/", views.site_crew, name="crew"),
    path("sites/<int:pk>/crew/assign/", views.site_assign, name="assign"),
    path(
        "sites/<int:pk>/crew/<int:assignment_id>/end/",
        views.end_assignment,
        name="end_assignment",
    ),
    path("sites/<int:pk>/attendance/", views.site_attendance, name="attendance"),
    path("sites/<int:pk>/pay/", views.site_pay, name="pay"),
    path("sites/<int:pk>/settings/", views.site_settings, name="settings"),
]
