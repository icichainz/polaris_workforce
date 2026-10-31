"""Worker portal forms: sign-in, the worker's own PIN, and the office's PIN form."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.services import is_weak_pin


def _pin_widget(autocomplete: str) -> forms.PasswordInput:
    return forms.PasswordInput(
        attrs={
            "inputmode": "numeric",
            "autocomplete": autocomplete,
            "pattern": r"\d{4}",
            "maxlength": "4",
        }
    )


def _pin_field(label, *, autocomplete="new-password", required=True) -> forms.RegexField:
    return forms.RegexField(
        label=label,
        regex=r"^\d{4}$",
        max_length=4,
        strip=True,
        required=required,
        error_messages={"invalid": _("Enter exactly 4 digits.")},
        widget=_pin_widget(autocomplete),
    )


class TelInput(forms.TextInput):
    input_type = "tel"


class SignInForm(forms.Form):
    phone = forms.CharField(
        label=_("Phone"),
        max_length=32,
        strip=True,
        widget=TelInput(
            attrs={"autocomplete": "tel", "autofocus": True, "placeholder": "+228 90 00 00 00"}
        ),
    )
    pin = _pin_field(_("PIN"), autocomplete="current-password")


class _NewPinMixin:
    """``pin`` + ``pin_confirm``: weak PINs refused, both must match."""

    def clean_pin(self):
        pin = self.cleaned_data.get("pin", "")
        if pin and is_weak_pin(pin):
            raise forms.ValidationError(_("Too easy to guess: avoid 1111 or 1234."))
        return pin

    def clean(self):
        cleaned = super().clean()
        pin, confirm = cleaned.get("pin"), cleaned.get("pin_confirm", "")
        if pin and pin != confirm:
            self.add_error("pin_confirm", _("The two PINs are different."))
        return cleaned


class ChangePinForm(_NewPinMixin, forms.Form):
    current_pin = _pin_field(_("Current PIN"), autocomplete="current-password")
    pin = _pin_field(_("New PIN"))
    pin_confirm = forms.CharField(
        label=_("Repeat the PIN"), max_length=4, widget=_pin_widget("new-password")
    )


class OfficePinForm(_NewPinMixin, forms.Form):
    """Give access or reset the PIN. Leave the PIN empty to get a random one."""

    pin = _pin_field(_("New PIN"), required=False)
    pin_confirm = forms.CharField(
        label=_("Repeat the PIN"),
        max_length=4,
        required=False,
        widget=_pin_widget("new-password"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pin"].help_text = _("Leave empty to get a random PIN.")
