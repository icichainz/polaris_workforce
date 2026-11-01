"""Office screen *AI assistant access*: create, list and revoke agent tokens.

Office roles (admin, site manager, accountant) manage their own tokens; admins also see
and revoke every token of the company. A new token is shown once, on the response to the
POST that created it, never cached and never stored in clear or in the session.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import is_admin, role_required

from . import services
from .forms import AgentTokenForm

NAV = "agents:access"


def _page(request, form=None, status=200):
    user = request.user
    allowed = services.may_hold_token(user)
    tokens = services.manageable_tokens(user)
    context = {
        "allowed": allowed,
        "form": form or AgentTokenForm(may_approve=services.may_approve(user)),
        "my_tokens": [t for t in tokens if t.user_id == user.pk],
        "other_tokens": [t for t in tokens if t.user_id != user.pk] if is_admin(user) else [],
        "nav_active": NAV,
    }
    return render(request, "agents/access.html", context, status=status)


@role_required(*services.OFFICE_ROLES)
@require_GET
def access(request):
    return _page(request)


@role_required(*services.OFFICE_ROLES)
@require_POST
def create(request):
    user = request.user
    if not services.may_hold_token(user):
        raise Http404
    form = AgentTokenForm(request.POST, may_approve=services.may_approve(user))
    if not form.is_valid():
        return _page(request, form=form, status=400)
    token, raw = services.create_token(
        user,
        name=form.cleaned_data["name"],
        can_approve=form.cleaned_data.get("can_approve", False),
        lifetime_days=form.cleaned_data["lifetime_days"],
    )
    # Shown once: this response only, never cached, never in the session.
    response = render(
        request,
        "agents/token_shown.html",
        {
            "token": token,
            "raw": raw,
            "server_url": request.build_absolute_uri("/").rstrip("/"),
            "nav_active": NAV,
        },
    )
    add_never_cache_headers(response)
    return response


@role_required(*services.OFFICE_ROLES)
@require_POST
def revoke(request, pk):
    token = services.manageable_tokens(request.user).filter(pk=pk).first()
    if token is None:
        raise Http404
    if services.revoke_token(request.user, token):
        messages.success(request, _("“%(name)s” can no longer use Polaris.") % {"name": token.name})
    return redirect(NAV)
