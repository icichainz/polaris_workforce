from django import forms
from django.utils.translation import gettext_lazy as _

from .services import DEFAULT_LIFETIME_DAYS, LIFETIMES_DAYS

LIFETIME_LABELS = {
    30: _("30 days"),
    90: _("90 days"),
    365: _("1 year"),
}


class AgentTokenForm(forms.Form):
    name = forms.CharField(
        label=_("Name"),
        max_length=100,
        help_text=_("Where it is used, e.g. “Claude on my laptop”."),
    )
    lifetime_days = forms.TypedChoiceField(
        label=_("Works for"),
        choices=[(days, LIFETIME_LABELS[days]) for days in LIFETIMES_DAYS],
        coerce=int,
        initial=DEFAULT_LIFETIME_DAYS,
    )
    can_approve = forms.BooleanField(
        label=_("May approve or send back roll calls (always after a preview)"),
        required=False,
    )

    def __init__(self, *args, may_approve: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if not may_approve:
            del self.fields["can_approve"]
