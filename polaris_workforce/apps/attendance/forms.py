from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AttendanceDay


class BulkApproveForm(forms.Form):
    """Several days ticked on the Approvals screen. ``days`` is limited to the days the
    user may see (the view passes them); any other id makes the form invalid."""

    MAX_DAYS = 200

    days = forms.ModelMultipleChoiceField(
        queryset=AttendanceDay.objects.none(),
        error_messages={"required": _("Select at least one day.")},
    )

    def __init__(self, *args, days, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["days"].queryset = days

    def clean_days(self):
        days = self.cleaned_data["days"]
        if len(days) > self.MAX_DAYS:
            raise forms.ValidationError(
                _("Select at most %(max)s days at a time."),
                code="too_many",
                params={"max": self.MAX_DAYS},
            )
        return days

    @property
    def out_of_scope(self) -> bool:
        """True when an id is not one of the user's days (another site, another company)."""
        return self.has_error("days", "invalid_choice") or self.has_error(
            "days", "invalid_pk_value"
        )


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
