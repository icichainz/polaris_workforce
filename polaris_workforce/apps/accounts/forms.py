"""Forms for the Users & roles screen, and the office sign-in forms."""

import math

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.core.choices import Language
from apps.worksites.models import Site

from . import services
from .models import User
from .permissions import scoped_sites

Role = User.Role


class UserForm(forms.ModelForm):
    """Invite or edit a user: identity, role, language and site assignments."""

    managed_sites = forms.ModelMultipleChoiceField(
        label=_("Sites they manage"),
        queryset=Site.objects.none(),
        required=False,
        help_text=_("For site managers. A site has one manager; choosing it here replaces it."),
    )
    foreman_sites = forms.ModelMultipleChoiceField(
        label=_("Sites they are foreman on"),
        queryset=Site.objects.none(),
        required=False,
        help_text=_("For foremen."),
    )
    password = forms.CharField(
        label=_("Temporary password"),
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_(
            "Office roles sign in with a password. Leave empty and they can use "
            "“Forgot?” on the sign-in page."
        ),
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "phone",
            "role",
            "language",
            "is_active",
        ]
        labels = {"is_active": _("Active (may sign in)")}

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        # The admin's company's sites (a new user joins the admin's company).
        sites = scoped_sites(actor).order_by("name") if actor else Site.objects.none()
        self.fields["managed_sites"].queryset = sites
        self.fields["foreman_sites"].queryset = sites
        self.fields["first_name"].required = True
        self.fields["role"].widget.attrs["x-model"] = "role"
        self.fields["phone"].widget.attrs.update({"autocomplete": "tel", "inputmode": "tel"})
        self.fields["phone"].help_text = _("Foremen sign in on the phone with this number.")
        self.fields["language"].choices = [("", _("Company language")), *Language.choices]
        self.fields["username"].help_text = _(
            "Used to sign in to the office. Letters, digits and @ . + - _ only."
        )
        if self.instance.pk:
            del self.fields["password"]
            self.initial["managed_sites"] = list(Site.objects.filter(manager=self.instance))
            self.initial["foreman_sites"] = list(self.instance.foreman_sites.all())
            self.fields["is_active"].help_text = _(
                "A disabled user cannot sign in, in the office or on the phone."
            )
        else:
            del self.fields["is_active"]

    @property
    def editing_self(self) -> bool:
        return (
            self.instance.pk is not None
            and self.actor is not None
            and (self.instance.pk == self.actor.pk)
        )

    def clean_phone(self):
        # Unique across every company: the phone signs in with phone + PIN only.
        phone = self.cleaned_data["phone"].strip()
        if phone and services.users_with_phone(phone, exclude_pk=self.instance.pk):
            raise forms.ValidationError(_("Another user already has this phone number."))
        return phone

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        if role == Role.FOREMAN and not cleaned.get("phone"):
            self.add_error("phone", _("Foremen need a phone number to sign in."))
        if self.editing_self:
            if role and role != self.actor.role and not self.actor.is_superuser:
                self.add_error("role", _("You cannot change your own role."))
            if cleaned.get("is_active") is False:
                self.add_error("is_active", _("You cannot disable your own account."))
        return cleaned

    def save_user(self, *, reason: str = "") -> User:
        user = self.save(commit=False)
        return services.save_user(
            user,
            actor=self.actor,
            managed_sites=self.cleaned_data["managed_sites"],
            foreman_sites=self.cleaned_data["foreman_sites"],
            password=self.cleaned_data.get("password", ""),
            reason=reason,
        )


class PinForm(forms.Form):
    """Set or reset a foreman's 4-digit PIN."""

    pin = forms.RegexField(
        label=_("New PIN"),
        regex=r"^\d{4}$",
        max_length=4,
        strip=True,
        error_messages={"invalid": _("Enter exactly 4 digits.")},
        widget=forms.PasswordInput(
            attrs={
                "inputmode": "numeric",
                "autocomplete": "new-password",
                "pattern": r"\d{4}",
                "maxlength": "4",
            }
        ),
    )
    pin_confirm = forms.CharField(
        label=_("Repeat the PIN"),
        max_length=4,
        widget=forms.PasswordInput(
            attrs={
                "inputmode": "numeric",
                "autocomplete": "new-password",
                "pattern": r"\d{4}",
                "maxlength": "4",
            }
        ),
    )

    def clean_pin(self):
        pin = self.cleaned_data["pin"]
        if services.is_weak_pin(pin):
            raise forms.ValidationError(_("Too easy to guess: avoid 1111 or 1234."))
        return pin

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("pin") and cleaned.get("pin") != cleaned.get("pin_confirm"):
            self.add_error("pin_confirm", _("The two PINs are different."))
        return cleaned


# --- Office sign-in with a lockout (/accounts/login/ and /admin/login/) -------------------


class SignInLimitMixin:
    """Refuses a (username, IP) after too many wrong passwords (see
    ``services.record_failed_login``). Mixed into Django's sign-in forms."""

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        ip = services.login_client_ip(self.request)
        locked_until = services.login_locked_until(username, ip)
        if locked_until is not None:
            raise self._locked_error(locked_until)
        try:
            cleaned = super().clean()
        except forms.ValidationError:
            # Only a wrong username/password counts (not "inactive" or "not staff").
            if username and self.cleaned_data.get("password") and self.user_cache is None:
                locked_until = services.record_failed_login(username, ip)
                if locked_until is not None:
                    raise self._locked_error(locked_until) from None
            raise
        services.clear_failed_logins(username, ip)
        return cleaned

    @staticmethod
    def _locked_error(locked_until) -> forms.ValidationError:
        minutes = max(1, math.ceil((locked_until - timezone.now()).total_seconds() / 60))
        return forms.ValidationError(
            ngettext(
                "Too many failed sign-in attempts. Try again in %(minutes)d minute.",
                "Too many failed sign-in attempts. Try again in %(minutes)d minutes.",
                minutes,
            ),
            code="locked",
            params={"minutes": minutes},
        )


class SignInForm(SignInLimitMixin, AuthenticationForm):
    """``/accounts/login/``."""


class AdminSignInForm(SignInLimitMixin, AdminAuthenticationForm):
    """``/admin/login/``."""
