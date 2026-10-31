from django import forms
from django.utils.translation import gettext_lazy as _


class DecisionForm(forms.Form):
    """Approve or send back a submitted day. The note is required only to send back
    (checked by ``services.reject_day``), so the field itself is optional."""

    APPROVE = "approve"
    REJECT = "reject"

    action = forms.ChoiceField(choices=[(APPROVE, _("Approve day")), (REJECT, _("Send back"))])
    note = forms.CharField(
        label=_("Note to foreman"),
        help_text=_("Required to send the day back."),
        max_length=500,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": _("e.g. Kodjo was seen on site — please check")}
        ),
    )
