"""Who may see and do what — the single place for role and site-scoping rules.

Every office view, queryset and API endpoint that touches site data goes through
these helpers (CLAUDE.md rule 8). Wave 2 agent A owns and extends this module;
the other areas import from it.

Rules:
- Admins (and superusers) see every site and may do everything.
- Accountants see every site, read-only, plus pay-period locking and reports.
- Site managers see the sites they manage.
- Foremen see the sites they are a foreman on.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
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


class IsForeman(BasePermission):
    """API: only foremen (and admins, for support) may use the phone endpoints."""

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, Role.FOREMAN, Role.ADMIN)
