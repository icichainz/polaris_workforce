from django.urls import path

from . import views

app_name = "worksites"

urlpatterns = [
    path("sites/", views.site_list, name="list"),
]
