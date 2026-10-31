"""Who may see and do what — the single place for role and site-scoping rules.

Every office view, queryset and API endpoint that touches site data goes through
these helpers (CLAUDE.md rule 8). Wave 2 agent A owns and extends this module;
the other areas import from it.

Rules:
- Admins (and superusers) see every site and may do everything.
- Accountants see every site, read-only, plus pay-period locking and reports.
- Site managers see the sites they manage (and may lock their pay periods).
- Foremen see the sites they are a foreman on.

What each role may do is the ``PERMISSIONS`` table below (the matrix shown on the Users
screen). Check it with ``can(user, action, site)`` or ``user.can(action, site)``.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import BasePermission

from apps.worksites.models import Site

from .models import User

Role = User.Role


def has_role(user, *roles: str) -> bool:
    """True if the user is signed in and has one of the roles (superusers count as admin)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser:
        return Role.ADMIN in roles or not roles
    return user.role in roles


def is_admin(user) -> bool:
    return has_role(user, Role.ADMIN)


def scoped_sites(user):
    """The sites this user may see."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return Site.objects.none()
    if user.is_superuser or user.role in (Role.ADMIN, Role.ACCOUNTANT):
        return Site.objects.all()
    if user.role == Role.SITE_MANAGER:
        return Site.objects.filter(manager=user)
    if user.role == Role.FOREMAN:
        return Site.objects.filter(foremen=user)
    return Site.objects.none()


def can_view_site(user, site) -> bool:
    return scoped_sites(user).filter(pk=getattr(site, "pk", site)).exists()


def can_manage_site(user, site) -> bool:
    """Approve days, pay out, edit crew: admins anywhere, managers on their own sites."""
    if is_admin(user):
        return True
    return has_role(user, Role.SITE_MANAGER) and can_view_site(user, site)


def get_site_or_403(user, site_id) -> Site:
    """Fetch a site the user may see, else raise PermissionDenied (403 in views and the API).

    A site that doesn't exist also gives 403, so ids outside your scope can't be probed.
    """
    site = scoped_sites(user).filter(pk=site_id).first()
    if site is None:
        raise PermissionDenied
    return site


# --- What each role may do -----------------------------------------------------------

# Scope of a permission for a role.
ALL = "all"  # on every site
OWN = "own"  # only on the sites the user is assigned to

TAKE_ROLL_CALL = "take_roll_call"
CORRECT_DAY = "correct_day"
APPROVE_DAYS = "approve_days"
PAY_OUT = "pay_out"
GIVE_ADVANCES = "give_advances"
EDIT_WORKERS = "edit_workers"
LOCK_PERIODS = "lock_periods"
VIEW_REPORTS = "view_reports"
MANAGE_COMPANY = "manage_company"

# action → (label, {role: ALL | OWN}); a role that is missing may not do it.
PERMISSIONS: dict[str, tuple] = {
    TAKE_ROLL_CALL: (
        _("Take roll call"),
        {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN, Role.FOREMAN: OWN},
    ),
    CORRECT_DAY: (
        _("Correct a submitted day (with reason)"),
        {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN, Role.FOREMAN: OWN},
    ),
    APPROVE_DAYS: (_("Approve days"), {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN}),
    PAY_OUT: (
        _("Pay out cash & record proof"),
        {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN, Role.FOREMAN: OWN},
    ),
    GIVE_ADVANCES: (_("Give advances"), {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN}),
    EDIT_WORKERS: (_("Edit workers & pay rates"), {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN}),
    LOCK_PERIODS: (
        _("Close & lock pay periods"),
        {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN, Role.ACCOUNTANT: ALL},
    ),
    VIEW_REPORTS: (
        _("Reports & exports"),
        {Role.ADMIN: ALL, Role.SITE_MANAGER: OWN, Role.ACCOUNTANT: ALL},
    ),
    MANAGE_COMPANY: (_("Company settings & users"), {Role.ADMIN: ALL}),
}

MATRIX_ROLES = [Role.ADMIN, Role.SITE_MANAGER, Role.FOREMAN, Role.ACCOUNTANT]


def _role_of(user) -> str | None:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    return Role.ADMIN if user.is_superuser else user.role


def can(user, action: str, site=None) -> bool:
    """May ``user`` do ``action`` (on ``site``)?

    Without a site, an ``OWN`` permission answers "may do it on their own sites", which
    is what an office screen checks before listing anything; pass the site to check one.
    Unknown actions raise ``KeyError``.
    """
    _label, scopes = PERMISSIONS[action]
    scope = scopes.get(_role_of(user))
    if scope is None:
        return False
    if scope == ALL or site is None:
        return True
    return can_view_site(user, site)


def permission_matrix() -> list[dict]:
    """Rows for the Users screen: ``{"label", "cells": [ALL | OWN | None, …]}`` per action,
    one cell per role in ``MATRIX_ROLES`` order."""
    return [
        {"action": action, "label": label, "cells": [scopes.get(role) for role in MATRIX_ROLES]}
        for action, (label, scopes) in PERMISSIONS.items()
    ]


def can_lock_periods(user, site=None) -> bool:
    """Close and lock pay periods: admins and accountants everywhere, site managers on
    their own sites. Payroll checks it with the period's site."""
    return can(user, LOCK_PERIODS, site)


def can_view_reports(user, site=None) -> bool:
    """Reports and exports: admins and accountants everywhere, managers on their sites."""
    return can(user, VIEW_REPORTS, site)


def can_manage_users(user) -> bool:
    """Company settings and the Users screen: admins only."""
    return can(user, MANAGE_COMPANY)


def role_required(*roles: str):
    """Office view decorator: sign-in required, then one of the roles (403 otherwise)."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, *roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


class RoleRequiredMixin(AccessMixin):
    """Class-based office views: like ``role_required``. Set ``allowed_roles``."""

    allowed_roles: tuple[str, ...] = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not has_role(request.user, *self.allowed_roles):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class IsForeman(BasePermission):
    """API: only foremen (and admins, for support) may use the phone endpoints."""

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, Role.FOREMAN, Role.ADMIN)
