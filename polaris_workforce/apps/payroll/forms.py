"""Office forms for payroll. Money is typed in major units ("10 000") and converted to
the currency's smallest unit through ``apps.core.money``; never a float."""

import uuid
from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.choices import ProofType
from apps.core.models import CompanySettings
from apps.core.money import from_minor, to_minor

from .models import (
    DeductionRule,
    PayRuleSettings,
    RulePayType,
    SitePayRules,
    StoppagePay,
    TaskType,
)


class MoneyField(forms.CharField):
    """A money amount typed in major units; ``cleaned_data`` is an int of minor units."""

    def __init__(self, *, decimals: int = 0, **kwargs):
        self.decimals = decimals
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "numeric"}))
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{from_minor(value, self.decimals):f}"
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        text = value.replace(" ", "").replace(" ", "").replace("\xa0", "")
        text = text.replace(",", ".")
        try:
            amount = to_minor(text, self.decimals)
        except (ValueError, TypeError) as exc:
            raise forms.ValidationError(
                _("Enter an amount such as 10 000."), code="invalid"
            ) from exc
        if amount < 0:
            raise forms.ValidationError(_("The amount cannot be negative."), code="negative")
        return amount


def proof_choices(company: CompanySettings):
    accepted = list(company.accepted_proofs or [])
    choices = [(value, label) for value, label in ProofType.choices if value in accepted]
    if not accepted or ProofType.NONE in accepted:
        choices.append((ProofType.NONE, ProofType.NONE.label))
    return choices


class ProofFormMixin(forms.Form):
    proof_type = forms.ChoiceField(label=_("Proof of receipt"))
    proof_file = forms.FileField(
        label=_("Proof file"),
        required=False,
        help_text=_("Photo of the worker, signature or thumb mark (JPEG, PNG or PDF, ≤ 5 MB)."),
    )

    def __init__(self, *args, company: CompanySettings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["proof_type"].choices = proof_choices(company)


class MarkPaidForm(ProofFormMixin):
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class UndoPaymentForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)


class CashIssueForm(forms.Form):
    amount = MoneyField(label=_("Cash issued"))
    note = forms.CharField(label=_("Note"), required=False, max_length=500)

    def __init__(self, *args, company: CompanySettings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].decimals = company.currency_decimals


class LockForm(forms.Form):
    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Required when some workers are not paid."),
    )


class AdvanceForm(ProofFormMixin):
    worker = forms.ModelChoiceField(label=_("Worker"), queryset=None)
    amount = MoneyField(label=_("Amount"))
    deduction_per_pay = MoneyField(label=_("Deduct per pay"))
    reason = forms.CharField(label=_("Reason"), required=False, max_length=200)

    field_order = ["worker", "amount", "deduction_per_pay", "reason", "proof_type", "proof_file"]

    def __init__(self, *args, company: CompanySettings, workers, **kwargs):
        super().__init__(*args, company=company, **kwargs)
        self.fields["worker"].queryset = workers
        self.fields["worker"].label_from_instance = lambda w: f"{w.full_name} · {w.number}"
        for name in ("amount", "deduction_per_pay"):
            self.fields[name].decimals = company.currency_decimals
        self.fields["deduction_per_pay"].initial = company.default_advance_deduction


# --- pay rules (Wave 4) ----------------------------------------------------------------------

TIME_WIDGET = {"attrs": {"type": "time"}, "format": "%H:%M"}
DATE_WIDGET = {"attrs": {"type": "date"}, "format": "%Y-%m-%d"}


def _check_night_window(form, cleaned):
    start, end = cleaned.get("night_start"), cleaned.get("night_end")
    if (start is None) != (end is None):
        form.add_error("night_end", _("Set both night times, or neither."))
    elif start is not None and start == end:
        form.add_error("night_end", _("The night must end at another time than it starts."))
    return cleaned


class PayRulesForm(forms.ModelForm):
    stoppage_amount = MoneyField(label=_("Stoppage day amount"), required=False)

    class Meta:
        model = PayRuleSettings
        fields = [
            "sunday_multiplier",
            "holiday_multiplier",
            "night_start",
            "night_end",
            "night_multiplier",
            "stoppage_pay",
            "stoppage_percent",
            "stoppage_amount",
        ]
        widgets = {
            "night_start": forms.TimeInput(**TIME_WIDGET),
            "night_end": forms.TimeInput(**TIME_WIDGET),
        }

    def __init__(self, *args, company: CompanySettings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stoppage_amount"].decimals = company.currency_decimals
        self.fields["stoppage_amount"].help_text = _("Per worker present, when fixed.")

    def clean_stoppage_amount(self):
        return self.cleaned_data.get("stoppage_amount") or 0

    def clean(self):
        return _check_night_window(self, super().clean())


class SitePayRulesForm(forms.ModelForm):
    class Meta:
        model = SitePayRules
        fields = list(SitePayRules.OVERRIDES)
        widgets = {
            "night_start": forms.TimeInput(**TIME_WIDGET),
            "night_end": forms.TimeInput(**TIME_WIDGET),
        }
        help_texts = {
            "sunday_multiplier": _("Empty: the company's."),
            "holiday_multiplier": _("Empty: the company's."),
            "night_multiplier": _("Empty: the company's."),
        }

    def clean(self):
        return _check_night_window(self, super().clean())


class DeductionRuleForm(forms.ModelForm):
    base_floor = MoneyField(label=_("Floor"), required=False)
    base_ceiling = MoneyField(label=_("Ceiling"), required=False)
    pay_types = forms.MultipleChoiceField(
        label=_("Applies to"),
        choices=RulePayType.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Leave all unticked for every pay type."),
    )

    class Meta:
        model = DeductionRule
        fields = [
            "name",
            "employee_percent",
            "employer_percent",
            "base_floor",
            "base_ceiling",
            "pay_types",
            "active_from",
            "active_to",
            "order",
        ]
        widgets = {
            "active_from": forms.DateInput(**DATE_WIDGET),
            "active_to": forms.DateInput(**DATE_WIDGET),
        }

    def __init__(self, *args, company: CompanySettings, rules, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules = rules  # the company's rules (for the unique name)
        for name in ("base_floor", "base_ceiling"):
            self.fields[name].decimals = company.currency_decimals
        self.fields["base_floor"].help_text = _(
            "No deduction on a pay whose gross is under this amount. Empty: none."
        )
        self.fields["base_ceiling"].help_text = _(
            "Only the gross up to this amount counts. Empty: no limit."
        )

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        taken = self.rules.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError(_("There is already a rule with this name."))
        return name

    def clean(self):
        cleaned = super().clean()
        floor, ceiling = cleaned.get("base_floor"), cleaned.get("base_ceiling")
        if floor is not None and ceiling is not None and ceiling < floor:
            self.add_error("base_ceiling", _("The ceiling cannot be under the floor."))
        start, end = cleaned.get("active_from"), cleaned.get("active_to")
        if start and end and end < start:
            self.add_error("active_to", _("The end cannot be before the start."))
        cleaned["pay_types"] = list(cleaned.get("pay_types") or [])
        return cleaned

    def validate_unique(self):
        """The company is set by the service; the name is checked in ``clean_name``."""


class TaskTypeForm(forms.ModelForm):
    unit_rate = MoneyField(label=_("Rate per unit"))

    class Meta:
        model = TaskType
        fields = ["name", "unit", "unit_rate", "is_active"]

    def __init__(self, *args, company: CompanySettings, tasks, **kwargs):
        super().__init__(*args, **kwargs)
        self.tasks = tasks
        self.fields["unit_rate"].decimals = company.currency_decimals

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if self.tasks.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_("There is already a task with this name."))
        return name

    def validate_unique(self):
        """The company is set by the service; the name is checked in ``clean_name``."""


def _worker_field(workers):
    field = forms.ModelChoiceField(label=_("Worker"), queryset=workers)
    field.label_from_instance = lambda w: f"{w.full_name} · {w.number}"
    return field


class PieceWorkForm(forms.Form):
    worker = forms.ModelChoiceField(label=_("Worker"), queryset=None)
    task = forms.ModelChoiceField(label=_("Task"), queryset=None)
    quantity = forms.DecimalField(
        label=_("Quantity"), min_value=Decimal("0.01"), max_digits=10, decimal_places=2
    )
    note = forms.CharField(label=_("Note"), required=False, max_length=200)

    def __init__(self, *args, workers, tasks, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["worker"] = _worker_field(workers)
        self.fields["task"].queryset = tasks
        self.order_fields(["worker", "task", "quantity", "note"])


class BonusForm(forms.Form):
    worker = forms.ModelChoiceField(label=_("Worker"), queryset=None)
    amount = MoneyField(label=_("Amount"))
    reason = forms.CharField(label=_("Reason"), max_length=200)

    def __init__(self, *args, workers, company: CompanySettings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["worker"] = _worker_field(workers)
        self.fields["amount"].decimals = company.currency_decimals
        self.order_fields(["worker", "amount", "reason"])


class StoppageForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=200)
    pay = forms.ChoiceField(label=_("Pay for workers present"), choices=StoppagePay.choices)
    percent = forms.DecimalField(
        label=_("Share of the day (%)"),
        required=False,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        max_digits=5,
        decimal_places=2,
    )
    amount = MoneyField(label=_("Fixed amount"), required=False)

    def __init__(self, *args, company: CompanySettings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].decimals = company.currency_decimals


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)
