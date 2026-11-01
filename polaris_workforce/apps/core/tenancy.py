"""Several companies on one installation: the current company.

Every record belongs to one ``Company``: directly (``company`` on users, sites, workers,
trades and audit entries) or through one of those (a day through its site, a payment
through its pay line's site, an advance through its worker…). One request, command or
test works for **one** company, the *current company*, kept in a context variable:

- Requests (office and API): ``CurrentCompanyMiddleware`` makes the request's user decide
  it: ``request.user.company``. The user is read when the company is first needed, so it
  works the same for a browser session, a phone's device token (DRF authenticates after
  the middleware ran and sets ``request.user``) and tests' ``force_authenticate``.
  Platform admins (superusers without a company) pick one with the company switcher; the
  choice is kept in the session (``SESSION_KEY``). Anonymous requests have none.
- ``POST /auth/pin/`` (anonymous) finds the user by phone number first (phones are unique
  across companies), then works inside ``use_company(user.company)``.
- Management commands and scripts: ``with use_company(company): …``.
- Tests: an autouse fixture in ``tests/conftest.py`` makes a default company current.

``CompanySettings.load()``, ``company_today()`` and ``resolve_setting()`` read the current
company's settings; new users, sites, workers and trades default to the current company.
Code that needs a company and runs without one raises ``NoCurrentCompany`` instead of
mixing companies. Querysets are scoped by the user's company in
``apps.accounts.permissions`` (``scoped_sites``, ``company_workers``, ``company_users``).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .models import Company

SESSION_KEY = "polaris_company_id"


class _RequestCompany:
    """The company of a request's user, worked out when first needed (and again if
    ``request.user`` changes, e.g. after DRF authenticated a device token)."""

    def __init__(self, request):
        self.request = request
        self._user = None
        self._company = None
        self.memo: dict = {}

    def resolve(self) -> Company | None:
        user = getattr(self.request, "user", None)
        if user is not self._user:
            self._user = user
            self._company = company_for_request(self.request)
            self.memo = {}
        return self._company


_current: contextvars.ContextVar[Company | _RequestCompany | None] = contextvars.ContextVar(
    "polaris_current_company", default=None
)


class NoCurrentCompany(RuntimeError):
    """Company data was read or written without a current company."""


def current_company_or_none() -> Company | None:
    value = _current.get()
    if isinstance(value, _RequestCompany):
        return value.resolve()
    return value


def current_company() -> Company:
    """The company this request, command or test works for (raises if there is none)."""
    company = current_company_or_none()
    if company is None:
        raise NoCurrentCompany(
            "No current company. Requests take it from the signed-in user; commands and "
            "scripts must run inside `with use_company(company):`."
        )
    return company


def current_company_id() -> int | None:
    """The current company's id, or ``None``. The default of every ``company`` field."""
    company = current_company_or_none()
    return company.pk if company is not None else None


def request_memo() -> dict | None:
    """A dict that lives as long as the current request (``None`` outside a request).

    For values read many times per request that only the request itself changes
    (``CompanySettings.load``). It is emptied when the request's user changes.
    """
    value = _current.get()
    if isinstance(value, _RequestCompany):
        value.resolve()
        return value.memo
    return None


def set_request_company(request) -> contextvars.Token:
    """Requests: the current company is the one of ``request.user`` (middleware)."""
    return _current.set(_RequestCompany(request))


def reset_current_company(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def use_company(company: Company | None):
    """Run a block for ``company`` (management commands, scripts, tests, PIN sign-in)."""
    token = _current.set(company)
    try:
        yield company
    finally:
        _current.reset(token)


# --- users -----------------------------------------------------------------------------


def is_platform_admin(user) -> bool:
    """A superuser who belongs to no company: manages companies and picks one to view."""
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and user.is_superuser
        and user.company_id is None
    )


def company_id_of(user) -> int | None:
    """The company whose data ``user`` works on: their own company, or for a platform
    admin the current (selected) company. ``None`` for anyone else."""
    if not getattr(user, "is_authenticated", False):
        return None
    if user.company_id is not None:
        return user.company_id
    if user.is_superuser:
        return current_company_id()
    return None


def company_for_request(request) -> Company | None:
    """The company of the request's signed-in user (``None`` when anonymous).

    A platform admin works on the company chosen with the switcher, else the first one
    by name (remembered in the session).
    """
    from .models import Company

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if user.company_id is not None:
        return user.company
    if not user.is_superuser:
        return None
    session = getattr(request, "session", None)
    chosen = session.get(SESSION_KEY) if session is not None else None
    company = Company.objects.filter(pk=chosen).first() if chosen else None
    if company is None:
        company = Company.objects.order_by("name", "pk").first()
        if company is not None and session is not None:
            session[SESSION_KEY] = company.pk
    return company


def switch_company(request, company: Company) -> None:
    """Platform admins: view ``company`` from now on (kept in the session)."""
    request.session[SESSION_KEY] = company.pk
    value = _current.get()
    if isinstance(value, _RequestCompany):
        value._user = None  # worked out again on next use
