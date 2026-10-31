"""Office views for users and roles (admins only). Logic is in ``services.py``."""

from django.contrib import messages
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.worksites.models import Site

from . import services
from .forms import PinForm, UserForm
from .models import ForemanDevice, User
from .permissions import MATRIX_ROLES, Role, permission_matrix, role_required

admin_only = role_required(Role.ADMIN)
NAV_ACTIVE = "accounts:users"  # sub-pages keep "Users & roles" highlighted


def _user_rows():
    users = User.objects.order_by("first_name", "last_name", "username").prefetch_related(
        Prefetch("managed_sites", queryset=Site.objects.order_by("name")),
        Prefetch("foreman_sites", queryset=Site.objects.order_by("name")),
        "devices",
    )
    rows = []
    for user in users:
        role = Role.ADMIN if user.is_superuser else user.role
        if role == Role.SITE_MANAGER:
            sites = [site.name for site in user.managed_sites.all()]
        elif role == Role.FOREMAN:
            sites = [site.name for site in user.foreman_sites.all()]
        else:
            sites = None  # every site
        rows.append(
            {
                "user": user,
                "role": role,
                "role_label": Role(role).label,
                "sites": sites,
                "uses_pin": role == Role.FOREMAN,
                "last_active": services.last_active(user),
            }
        )
    return rows


@admin_only
def users(request):
    return render(
        request,
        "accounts/users.html",
        {
            "rows": _user_rows(),
            "matrix": permission_matrix(),
            "matrix_roles": [Role(role).label for role in MATRIX_ROLES],
        },
    )


@admin_only
def user_create(request):
    form = UserForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save_user()
        messages.success(request, _("%(name)s was added.") % {"name": user})
        return redirect("accounts:user_edit", pk=user.pk)
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "creating": True, "nav_active": NAV_ACTIVE},
    )


@admin_only
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user, actor=request.user)
    if request.method == "POST" and form.is_valid():
        form.save_user()
        messages.success(request, _("Changes saved."))
        return redirect("accounts:user_edit", pk=user.pk)
    return _edit_page(request, user, form=form)


def _edit_page(request, user, *, form=None, pin_form=None, status=200):
    # A form that failed validation has changed ``user`` in memory: show the saved state.
    user = User.objects.get(pk=user.pk)
    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form or UserForm(instance=user, actor=request.user),
            "pin_form": pin_form or PinForm(),
            "subject": user,
            "can_have_pin": services.can_have_pin(user),
            "devices": user.devices.all(),
            "creating": False,
            "nav_active": NAV_ACTIVE,
        },
        status=status,
    )


@admin_only
@require_POST
def user_set_pin(request, pk):
    user = get_object_or_404(User, pk=pk)
    if not services.can_have_pin(user):
        messages.error(request, _("Only foremen and admins sign in with a PIN."))
        return redirect("accounts:user_edit", pk=user.pk)
    pin_form = PinForm(request.POST)
    if not pin_form.is_valid():
        return _edit_page(request, user, pin_form=pin_form, status=400)
    resetting = user.has_pin
    services.set_pin(user, pin_form.cleaned_data["pin"], actor=request.user)
    if resetting:
        messages.success(request, _("PIN changed. Tell %(name)s the new PIN.") % {"name": user})
    else:
        messages.success(request, _("PIN set. Tell %(name)s the PIN.") % {"name": user})
    return redirect("accounts:user_edit", pk=user.pk)


@admin_only
@require_POST
def device_revoke(request, pk, device_pk):
    device = get_object_or_404(ForemanDevice, pk=device_pk, user_id=pk)
    if services.revoke_device(device, actor=request.user, reason="Revoked on the Users screen."):
        messages.success(request, _("The phone was signed out."))
    return redirect("accounts:user_edit", pk=pk)
