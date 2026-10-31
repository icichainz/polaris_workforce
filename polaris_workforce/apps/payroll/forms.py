"""Office forms for payroll. Money is typed in major units ("10 000") and converted to
the currency's smallest unit through ``apps.core.money``; never a float."""

import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.choices import ProofType
from apps.core.models import CompanySettings
from apps.core.money import from_minor, to_minor


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
