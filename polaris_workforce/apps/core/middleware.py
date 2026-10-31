"""Request middleware: the current company, and the office language.

Current company (``CurrentCompanyMiddleware``): see ``apps.core.tenancy``. For every
request it is the company of ``request.user`` (read when first needed, so a phone's device
token, authenticated later by DRF, counts too), and it is reset when the response is
ready. A platform admin without any company yet is sent to the Companies page.

Office language (``UserLanguageMiddleware``): signed-in users see the app in their own language.

Order of preference for a signed-in user: their ``User.language``, then a language they
picked with the switcher (the language cookie), then the company's
``CompanySettings.language``. The browser's ``Accept-Language`` is not used once signed in,
so the company setting actually applies. Signed-out pages (sign-in) keep Django's usual
choice: the language switcher cookie, then the browser. The API is unaffected: the phone sends
``Accept-Language`` and API views answer in it.
"""

from django.conf import settings
from django.shortcuts import redirect
from django.utils import translation

from apps.core import tenancy
from apps.core.models import CompanySettings
from apps.core.templatetags.polaris import _CACHE_ATTR

# Paths a platform admin may open before any company exists.
NO_COMPANY_PATHS = ("/companies/", "/admin/", "/accounts/", "/i18n/", "/api/", "/static/")


class CurrentCompanyMiddleware:
    """Must come after ``AuthenticationMiddleware`` (and before anything that reads
    company settings)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = tenancy.set_request_company(request)
        try:
            if (
                tenancy.is_platform_admin(request.user)
                and tenancy.current_company_or_none() is None
                and not request.path_info.startswith(NO_COMPANY_PATHS)
            ):
                return redirect("core:companies")
            return self.get_response(request)
        finally:
            tenancy.reset_current_company(token)


class UserLanguageMiddleware:
    """Must come after ``AuthenticationMiddleware`` and ``LocaleMiddleware``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = self._language_for(request)
        if language:
            translation.activate(language)
            request.LANGUAGE_CODE = language
        return self.get_response(request)

    @staticmethod
    def _language_for(request) -> str | None:
        if request.path.startswith("/api/"):
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if user.language:
            return user.language
        chosen = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if chosen in dict(settings.LANGUAGES):
            return chosen
        if tenancy.current_company_or_none() is None:
            return None
        # Shared with the {% money %} tag, so the settings are loaded once per request.
        company = getattr(request, _CACHE_ATTR, None)
        if company is None:
            company = CompanySettings.load()
            setattr(request, _CACHE_ATTR, company)
        return company.language or None
