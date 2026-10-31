"""Forms for the Users & roles screen."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.choices import Language
from apps.worksites.models import Site

from . import services
from .models import User

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
        sites = Site.objects.order_by("name")
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
