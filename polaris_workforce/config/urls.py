from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("apps.dashboard.urls")),
    # Office screens: each app owns its own urls.py (see the URL map in
    # docs/implementation-plan.md); Wave 2 agents never edit this file.
    path("", include("apps.core.urls")),
    path("", include("apps.attendance.urls")),
    path("", include("apps.payroll.urls")),
    path("", include("apps.workers.urls")),
    path("", include("apps.worksites.urls")),
    path("", include("apps.accounts.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("api/v1/", include("apps.api.urls")),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
