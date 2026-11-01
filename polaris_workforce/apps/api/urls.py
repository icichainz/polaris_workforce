"""``/api/v1/`` — mounted by ``config/urls.py``.

Each app's routes live in ``apps/<app>/api/urls.py`` and are included here exactly once.
Wave 2 agents edit only their own app's ``api/`` package, never this file.
"""

from django.conf import settings
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

# converters registers <isodate:...> before the includes below.
from . import converters, files, views  # noqa: F401

app_name = "api"

urlpatterns = [
    path("health/", views.health, name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    # Uploaded files behind the device token (rules in apps.core.files).
    path(
        "files/workers/<int:worker_id>/photo/",
        files.WorkerPhotoView.as_view(),
        name="file-worker-photo",
    ),
    path(
        "files/proofs/<int:payment_id>/",
        files.PaymentProofView.as_view(),
        name="file-payment-proof",
    ),
    path("", include("apps.accounts.api.urls")),
    path("", include("apps.worksites.api.urls")),
    path("", include("apps.attendance.api.urls")),
    path("", include("apps.payroll.api.urls")),
    path("", include("apps.checkin.api.urls")),
    path("", include("apps.payouts.api.urls")),
    path("", include("apps.agents.api.urls")),
]

if settings.DEBUG:
    urlpatterns += [
        path("docs/", SpectacularSwaggerView.as_view(url_name="api:schema"), name="docs"),
    ]

# Must stay last: any other URL under /api/v1/ (a typo, an impossible date such as
# 2026-02-30) gets the JSON error format instead of Django's HTML 404 page. Only paths
# ending in "/" match, so a missing trailing slash still gets APPEND_SLASH's redirect.
urlpatterns += [re_path(r"^(?:.*/)?$", views.unknown_url, name="not-found")]
