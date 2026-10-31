"""Office forms: messaging settings, the Messages filters and a person's preference."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Channel, Kind, Message, MessagingSettings

TIME_FORMAT = "%H:%M"


def _time_field(label):
    return forms.TimeField(
        label=label,
        required=False,
        input_formats=[TIME_FORMAT, "%H:%M:%S"],
        widget=forms.TimeInput(format=TIME_FORMAT, attrs={"type": "time"}),
    )


class MessagingSettingsForm(forms.ModelForm):
    quiet_hours_start = _time_field(_("Quiet hours from"))
    quiet_hours_end = _time_field(_("Quiet hours until"))

    class Meta:
        model = MessagingSettings
        fields = [
            "enabled",
            "sms_enabled",
            "whatsapp_enabled",
            "default_channel",
            "default_country_code",
            "require_opt_in",
            "quiet_hours_start",
            "quiet_hours_end",
            "notify_payslip",
            "notify_missing_roll_call",
            "notify_awaiting_approval",
            "approval_reminder_hours",
            "notify_unclosed_cash_sheet",
            "notify_advance_limit",
            "advance_limit_warn_percent",
        ]

    def clean_default_country_code(self):
        return (self.cleaned_data.get("default_country_code") or "").strip().lstrip("+")


class MessageFilterForm(forms.Form):
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("Any status")), *Message.Status.choices],
    )
    kind = forms.ChoiceField(
        label=_("Type"), required=False, choices=[("", _("Any type")), *Kind.choices]
    )
    date_from = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", _("The end date is before the start date."))
        return cleaned


class PreferenceForm(forms.Form):
    opted_in = forms.BooleanField(label=_("Gets text messages"), required=False)
    channel = forms.ChoiceField(
        label=_("Channel"),
        required=False,
        choices=[("", _("Company default")), *Channel.choices],
    )
