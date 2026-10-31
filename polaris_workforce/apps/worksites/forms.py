"""Site forms: identity (new / edit site), setting overrides, assigning workers."""

from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod
from apps.workers.fields import MoneyField
from apps.workers.forms import DateInput
from apps.workers.models import Worker

from .models import Site

User = get_user_model()


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%H:%M")
        super().__init__(**kwargs)


class SiteForm(forms.ModelForm):
    labour_budget = MoneyField(label=_("Labour budget"), required=False)

    class Meta:
        model = Site
        fields = ["name", "address", "status", "started_on", "manager", "foremen"]
        widgets = {"started_on": DateInput(), "foremen": forms.CheckboxSelectMultiple}

    def __init__(self, *args, decimals: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["labour_budget"].decimals = decimals
        if self.instance.pk:
            self.fields["labour_budget"].initial = self.instance.labour_budget
        # People of the site's company only (a new Site() belongs to the current company).
        people = User.objects.filter(company_id=self.instance.company_id, is_active=True)
        self.fields["manager"].queryset = people.filter(role=User.Role.SITE_MANAGER).order_by(
            "first_name", "last_name"
        )
        self.fields["manager"].empty_label = _("No site manager")
        self.fields["foremen"].queryset = people.filter(role=User.Role.FOREMAN).order_by(
            "first_name", "last_name"
        )


class SiteSettingsForm(forms.ModelForm):
    """The site's overrides of the company settings. Blank = use the company value."""

    pay_frequency = forms.TypedChoiceField(
        label=_("Pay frequency"), required=False, empty_value=None
    )
    payment_method = forms.TypedChoiceField(
        label=_("Payment method"), required=False, empty_value=None
    )

    class Meta:
        model = Site
        fields = [
            "pay_frequency",
            "pay_every_n_days",
            "payment_method",
            "work_start",
            "work_end",
            "late_after",
            "roll_call_due",
            "overtime_after_hours",
            "overtime_multiplier",
        ]
        widgets = {
            "work_start": TimeInput(),
            "work_end": TimeInput(),
            "late_after": TimeInput(),
            "roll_call_due": TimeInput(),
        }

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["pay_frequency"].choices = [
            ("", _("Company default (%(value)s)") % {"value": company.get_pay_frequency_display()}),
            *PayFrequency.choices,
        ]
        self.fields["payment_method"].choices = [
            (
                "",
                _("Company default (%(value)s)") % {"value": company.get_payment_method_display()},
            ),
            *PaymentMethod.choices,
        ]
        for name in ("pay_every_n_days", "work_start", "work_end", "late_after", "roll_call_due"):
            value = getattr(company, name)
            if value is None:
                text = _("Company default: not set. Leave blank to inherit.")
            else:
                shown = value.strftime("%H:%M") if hasattr(value, "strftime") else value
                text = _("Company default: %(value)s. Leave blank to inherit.") % {"value": shown}
            self.fields[name].help_text = text
        for name in ("overtime_after_hours", "overtime_multiplier"):
            self.fields[name].help_text = _(
                "Company default: %(value)s. Leave blank to inherit."
            ) % {"value": getattr(company, name).normalize()}

    def clean(self):
        cleaned = super().clean()
        frequency = cleaned.get("pay_frequency") or self.company.pay_frequency
        every = cleaned.get("pay_every_n_days") or self.company.pay_every_n_days
        if frequency == PayFrequency.EVERY_N_DAYS and not every:
            self.add_error("pay_every_n_days", _("Set the number of days between pay days."))
        start = cleaned.get("work_start") or self.company.work_start
        end = cleaned.get("work_end") or self.company.work_end
        if start and end and end <= start:
            self.add_error("work_end", _("Work must end after it starts."))
        return cleaned

    def changes(self) -> dict:
        return {name: self.cleaned_data.get(name) for name in self.Meta.fields}


class AssignWorkersForm(forms.Form):
    workers = forms.ModelMultipleChoiceField(
        label=_("Workers"),
        queryset=Worker.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Workers already on another site move here from the start date."),
    )
    start_date = forms.DateField(label=_("Starting"), widget=DateInput())

    def __init__(self, *args, workers, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["workers"].queryset = workers
        self.fields["start_date"].initial = today
