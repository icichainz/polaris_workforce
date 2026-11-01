"""Office forms for company settings and the audit log filters."""

import datetime
import zoneinfo
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.utils.translation import gettext_lazy as _

from .choices import WEEKDAY_ABBRS, ProofType
from .models import Company, CompanySettings
from .money import from_minor, to_minor

WEEKDAYS = list(enumerate(WEEKDAY_ABBRS, start=1))

# Suggestions for the currency input (any ISO 4217 code is accepted).
CURRENCY_SUGGESTIONS = [
    ("XOF", _("CFA franc (BCEAO) — no decimals")),
    ("XAF", _("CFA franc (BEAC) — no decimals")),
    ("GHS", _("Ghanaian cedi — 2 decimals")),
    ("NGN", _("Nigerian naira — 2 decimals")),
    ("EUR", _("Euro — 2 decimals")),
    ("USD", _("US dollar — 2 decimals")),
]

DECIMAL_CHOICES = [
    (0, _("0 — e.g. 12 500 XOF")),
    (1, "1"),
    (2, _("2 — e.g. 1 234.50 EUR")),
    (3, "3"),
    (4, "4"),
]

PROOF_CHOICES = [
    (ProofType.PHOTO, _("Photo of worker with cash")),
    (ProofType.SIGNATURE, _("Signature on phone")),
    (ProofType.THUMB_MARK, _("Thumb mark on paper sheet (photo)")),
    (ProofType.NONE, _("No proof needed")),
]

TIME_FORMAT = "%H:%M"


def _time_field(label):
    return forms.TimeField(
        label=label,
        input_formats=[TIME_FORMAT, "%H:%M:%S"],
        widget=forms.TimeInput(format=TIME_FORMAT, attrs={"type": "time"}),
    )


# Values as ``str(bool)``, so that a saved True / False shows as selected.
SWITCH_CHOICES = [("True", _("On")), ("False", _("Off"))]


def _switch_value(value) -> bool:
    return value in (True, "True")


def override_switch_field(label, inherit_label) -> forms.TypedChoiceField:
    """An on / off override of a company (or site) switch: blank inherits it.

    For the nullable ``BooleanField`` overrides on sites and workers (``tracking_enabled``).
    """
    return forms.TypedChoiceField(
        label=label,
        choices=[("", inherit_label), *SWITCH_CHOICES],
        coerce=_switch_value,
        empty_value=None,
        required=False,
    )


class CompanySettingsForm(forms.ModelForm):
    """Every section of the Settings screen, bound to the ``CompanySettings`` row.

    ``default_advance_deduction`` is typed in major units ("2 000", "12.50") and stored in
    minor units, using the decimal places chosen in the same form.
    """

    currency = forms.CharField(
        label=_("Currency"),
        max_length=3,
        validators=[RegexValidator(r"^[A-Z]{3}$", _("Use a three-letter ISO 4217 code."))],
        widget=forms.TextInput(
            attrs={"list": "currency-options", "autocomplete": "off", "maxlength": 3}
        ),
        help_text=_("ISO 4217 code, e.g. XOF, EUR."),
    )
    currency_decimals = forms.TypedChoiceField(
        label=_("Decimal places"), choices=DECIMAL_CHOICES, coerce=int
    )
    time_zone = forms.ChoiceField(label=_("Time zone"), choices=())
    work_start = _time_field(_("Work starts"))
    work_end = _time_field(_("Work ends"))
    late_after = _time_field(_("Late after"))
    roll_call_due = _time_field(_("Roll call due by"))
    half_day_units = forms.DecimalField(
        label=_("Half day counts as (days)"),
        min_value=Decimal("0"),
        max_value=Decimal("1"),
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.05", "inputmode": "decimal"}),
    )
    break_minutes = forms.IntegerField(label=_("Break (minutes)"), min_value=0, max_value=600)
    overtime_after_hours = forms.DecimalField(
        label=_("Overtime after (hours)"),
        min_value=Decimal("0"),
        max_value=Decimal("24"),
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.5", "inputmode": "decimal"}),
    )
    overtime_multiplier = forms.DecimalField(
        label=_("Overtime multiplier"),
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("1"), _("The overtime multiplier must be at least 1.")),
            MaxValueValidator(Decimal("10")),
        ],
        widget=forms.NumberInput(attrs={"step": "0.05", "inputmode": "decimal"}),
    )
    advance_limit_percent = forms.IntegerField(
        label=_("Advance limit (% of period pay)"),
        validators=[
            MinValueValidator(0, _("The advance limit must be between 0 and 100%%.")),
            MaxValueValidator(100, _("The advance limit must be between 0 and 100%%.")),
        ],
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    default_advance_deduction = forms.DecimalField(
        label=_("Default deduction per pay"), min_value=Decimal("0")
    )
    working_days = forms.TypedMultipleChoiceField(
        label=_("Working days"),
        choices=WEEKDAYS,
        coerce=int,
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": _("Pick at least one working day.")},
    )
    accepted_proofs = forms.MultipleChoiceField(
        label=_("Accepted proof of cash receipt"),
        choices=PROOF_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text=_(
            "“No proof needed” lets a payment be recorded without any proof of receipt. "
            "Untick it to require one of the other ticked proofs."
        ),
    )
    public_holidays = forms.CharField(
        label=_("Public holidays"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "2026-12-25 Christmas Day"}),
        help_text=_("One per line: the date (YYYY-MM-DD), then an optional name."),
    )
    tracking_start = _time_field(_("Tracking starts"))
    tracking_end = _time_field(_("Tracking ends"))

    class Meta:
        model = CompanySettings
        fields = [
            # Company & region
            "company_name",
            "country",
            "currency",
            "currency_decimals",
            "number_format",
            "time_zone",
            "language",
            # Attendance
            "attendance_mode",
            "half_day_units",
            "late_rule",
            "late_after",
            "roll_call_due",
            "record_times",
            "require_correction_reason",
            # Pay rules
            "pay_frequency",
            "pay_every_n_days",
            "default_pay_type",
            "work_start",
            "work_end",
            "break_minutes",
            "overtime_after_hours",
            "overtime_multiplier",
            # Payments & advances
            "payment_method",
            "accepted_proofs",
            "advance_limit_percent",
            "default_advance_deduction",
            # Working days & holidays
            "working_days",
            "public_holidays",
            # Presence tracking
            "tracking_enabled",
            "tracking_start",
            "tracking_end",
            "foreman_gps_interval_seconds",
            "presence_sightings_to_enter",
            "presence_arrival_window_seconds",
            "presence_absence_to_leave_seconds",
            "track_retention_days",
        ]
        labels = {
            "language": _("App language"),
            "late_rule": _("Late counts as"),
            "pay_frequency": _("Default pay frequency"),
            "payment_method": _("Default payment method"),
            "attendance_mode": _("Who records attendance"),
            "record_times": _("Record arrival and departure times"),
            "require_correction_reason": _("Corrections after submission need a reason"),
            "tracking_enabled": _("Track presence on the sites"),
        }
        widgets = {
            "attendance_mode": forms.RadioSelect,
            "payment_method": forms.RadioSelect,
            "pay_every_n_days": forms.NumberInput(attrs={"min": 1, "inputmode": "numeric"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        zones = sorted(zoneinfo.available_timezones() | {"UTC"})
        self.fields["time_zone"].choices = [(zone, zone) for zone in zones]
        self.fields["pay_every_n_days"].widget.attrs["min"] = 1
        company = self.instance
        if not self.is_bound:
            self.initial["default_advance_deduction"] = from_minor(
                company.default_advance_deduction, company.currency_decimals
            )
            self.initial["public_holidays"] = holidays_to_text(company.public_holidays)
        for name in ("attendance_mode", "payment_method", "late_rule", "default_pay_type"):
            # A radio list / select without the empty "---------" choice.
            field = self.fields[name]
            field.choices = [choice for choice in field.choices if choice[0] != ""]

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().upper()

    def clean_public_holidays(self):
        return text_to_holidays(self.cleaned_data.get("public_holidays") or "")

    def clean_accepted_proofs(self):
        chosen = set(self.cleaned_data["accepted_proofs"])
        return [value for value, _label in PROOF_CHOICES if value in chosen]

    def clean_working_days(self):
        return sorted(set(self.cleaned_data["working_days"]))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("work_start"), cleaned.get("work_end")
        if start and end and end <= start:
            self.add_error("work_end", _("Work must end after it starts."))
        late_after = cleaned.get("late_after")
        if start and late_after and late_after < start:
            self.add_error("late_after", _("Late after must not be before work starts."))
        track_start, track_end = cleaned.get("tracking_start"), cleaned.get("tracking_end")
        if track_start and track_end and track_end <= track_start:
            self.add_error("tracking_end", _("Tracking must end after it starts."))
        deduction = cleaned.get("default_advance_deduction")
        decimals = cleaned.get("currency_decimals")
        if deduction is not None and decimals is not None:
            try:
                cleaned["default_advance_deduction"] = to_minor(deduction, decimals)
            except ValueError:
                self.add_error(
                    "default_advance_deduction",
                    _("Use at most %(decimals)d decimal places.") % {"decimals": decimals},
                )
        return cleaned


def holidays_to_text(holidays) -> str:
    lines = []
    for entry in holidays or []:
        line = f"{entry.get('date', '')} {entry.get('name', '')}".strip()
        lines.append(line)
    return "\n".join(lines)


def text_to_holidays(text: str) -> list[dict]:
    """Parse "YYYY-MM-DD Name" lines into sorted ``{"date", "name"}`` entries."""
    holidays: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        date_text, _sep, name = line.partition(" ")
        try:
            date = datetime.date.fromisoformat(date_text)
        except ValueError as exc:
            raise forms.ValidationError(
                _("Line %(number)d: start with a date written YYYY-MM-DD."),
                params={"number": number},
            ) from exc
        holidays[date.isoformat()] = name.strip()
    return [{"date": date, "name": name} for date, name in sorted(holidays.items())]


class CompanyForm(forms.ModelForm):
    """Platform admins: add a company (its settings start with the defaults)."""

    class Meta:
        model = Company
        fields = ["name"]
        labels = {"name": _("Company name")}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Company.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError(_("A company with this name already exists."))
        return name


class AuditLogFilterForm(forms.Form):
    """Filters for the audit log viewer (all optional)."""

    actor = forms.ModelChoiceField(
        label=_("Who"), queryset=get_user_model().objects.none(), required=False
    )
    target_type = forms.ModelChoiceField(
        label=_("Target type"), queryset=ContentType.objects.none(), required=False
    )
    date_from = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, entries, **kwargs):
        """``entries``: the audit entries the viewer may see (their company's)."""
        super().__init__(*args, **kwargs)
        user_model = get_user_model()
        self.fields["actor"].queryset = user_model.objects.filter(
            pk__in=entries.exclude(actor=None).values("actor")
        ).order_by("first_name", "last_name", "username")
        self.fields["actor"].empty_label = _("Anyone")
        self.fields["target_type"].queryset = ContentType.objects.filter(
            pk__in=entries.exclude(content_type=None).values("content_type")
        ).order_by("app_label", "model")
        self.fields["target_type"].empty_label = _("Any type")
        self.fields["target_type"].label_from_instance = _content_type_label

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", _("The end date is before the start date."))
        return cleaned


def _content_type_label(content_type) -> str:
    model = content_type.model_class()
    if model is None:
        return f"{content_type.app_label}.{content_type.model}"
    return str(model._meta.verbose_name).capitalize()
