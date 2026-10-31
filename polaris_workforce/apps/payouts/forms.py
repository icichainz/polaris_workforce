"""Office forms for payouts. Every queryset is scoped in ``__init__`` (company, sites)."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from . import layouts
from .models import ExportLayout, PayoutAccount, TransferMethod


class PrepareForm(forms.Form):
    pay_date = forms.DateField(label=_("Pay day"), widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(label=_("Paid by"), choices=TransferMethod.choices)
    site = forms.ModelChoiceField(
        label=_("Site"), queryset=None, required=False, empty_label=_("All sites")
    )

    def __init__(self, *args, sites, all_sites_allowed: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites
        self.all_sites_allowed = all_sites_allowed
        if not all_sites_allowed:
            self.fields["site"].required = True
            self.fields["site"].empty_label = None

    def clean_site(self):
        site = self.cleaned_data.get("site")
        if site is None and not self.all_sites_allowed:
            raise ValidationError(_("Choose a site."))
        return site


class CreateBatchForm(PrepareForm):
    layout = forms.ModelChoiceField(label=_("Export layout"), queryset=None, required=False)

    def __init__(self, *args, layouts_qs, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["layout"].queryset = layouts_qs


class ExportForm(forms.Form):
    layout = forms.ModelChoiceField(label=_("Export layout"), queryset=None)

    def __init__(self, *args, layouts_qs, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["layout"].queryset = layouts_qs


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), required=False, max_length=300)


class ConfirmLineForm(forms.Form):
    provider_reference = forms.CharField(
        label=_("Transfer reference"), required=False, max_length=100
    )


class ImportForm(forms.Form):
    file = forms.FileField(
        label=_("Confirmation file"),
        help_text=_("The provider's report of the bulk payment (CSV or XLSX, ≤ 2 MB)."),
    )
    check_only = forms.BooleanField(
        label=_("Check only (change nothing)"), required=False, initial=False
    )


class PayoutAccountForm(forms.ModelForm):
    class Meta:
        model = PayoutAccount
        fields = [
            "method",
            "account_holder",
            "bank_name",
            "bank_code",
            "account_number",
            "mobile_operator",
            "mobile_number",
        ]


class ExportLayoutForm(forms.ModelForm):
    columns_text = forms.CharField(
        label=_("Columns"),
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}),
        help_text=_(
            "One column per line, in file order: a field name, optionally followed by "
            "“= Header text”. “text:VALUE” writes a fixed value, “blank” an empty column."
        ),
    )

    class Meta:
        model = ExportLayout
        fields = [
            "name",
            "method",
            "file_type",
            "header_row",
            "separator",
            "date_format",
            "amount_format",
            "encoding",
            "is_active",
        ]

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.company_id = company_id
        if self.instance.pk:
            self.fields["columns_text"].initial = layouts.columns_to_text(self.instance.columns)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        taken = ExportLayout.objects.filter(company_id=self.company_id, name__iexact=name)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise ValidationError(_("Another layout already has this name."))
        return name

    def clean_columns_text(self):
        columns = layouts.parse_columns(self.cleaned_data["columns_text"])
        self.instance.columns = columns
        return columns

    def _post_clean(self):
        # The company is set by the service; validate the rest of the model here.
        self.instance.company_id = self.company_id
        super()._post_clean()
