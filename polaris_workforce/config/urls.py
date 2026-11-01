from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.accounts.forms import AdminSignInForm, SignInForm

# Both sign-in pages lock a (username, IP) out after too many wrong passwords.
admin.site.login_form = AdminSignInForm

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
    # Wave 4: each new app owns its urls.py (office pages under its own prefix).
    path("", include("apps.payouts.urls")),
    path("", include("apps.messaging.urls")),
    path("", include("apps.checkin.urls")),
    path("", include("apps.agents.urls")),
    path("portal/", include("apps.portal.urls")),
    # Before django.contrib.auth.urls, so this sign-in view (with the lockout) wins.
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(authentication_form=SignInForm),
        name="login",
    ),
    path("accounts/", include("django.contrib.auth.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("api/v1/", include("apps.api.urls")),
    path("admin/", admin.site.urls),
]

# Unknown URLs under /api/ answer in the API error format; office pages keep HTML.
handler404 = "apps.api.views.page_not_found"

# No route serves MEDIA_ROOT, not even with DEBUG on: uploaded photos and proofs are
# downloaded through the scoped views in apps.core.files (core:* and api:file-*).
