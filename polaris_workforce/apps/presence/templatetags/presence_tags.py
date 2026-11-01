"""Presence cards on pages of other apps: ``{% presence_site_card site %}`` on the site
detail page and ``{% presence_worker_card worker %}`` on the worker profile.

Each card renders only for admins and site managers who may manage the site, or see the
worker (the presence screens' own rule, ``apps.presence.views``); for anyone else it
renders nothing. The page that includes it has already checked that the user may open it.
"""

from django import template

from apps.accounts.permissions import Role, can_manage_site, has_role
from apps.core import dates
from apps.core.settings import resolve_setting
from apps.workers.services import scoped_workers
from apps.worksites.models import Site

from .. import selectors
from ..models import BleBadge

register = template.Library()

MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)


def _user(context):
    request = context.get("request")
    return getattr(request, "user", None)


@register.inclusion_tag("presence/_site_card.html", takes_context=True)
def presence_site_card(context, site):
    user = _user(context)
    if user is None or not has_role(user, *MANAGERS) or not can_manage_site(user, site):
        return {"show": False}
    setup = selectors.site_setups(Site.objects.filter(pk=site.pk))[0]
    return {"show": True, "site": site, "setup": setup}


@register.inclusion_tag("presence/_worker_card.html", takes_context=True)
def presence_worker_card(context, worker):
    user = _user(context)
    if user is None or not has_role(user, *MANAGERS):
        return {"show": False}
    today = dates.company_today()
    if not scoped_workers(user, today).filter(pk=worker.pk).exists():
        return {"show": False}
    return {
        "show": True,
        "worker": worker,
        "today": today,
        "badge": BleBadge.objects.filter(worker=worker, active_to__isnull=True).first(),
        "tracking_on": bool(resolve_setting("tracking_enabled", worker=worker)),
    }
