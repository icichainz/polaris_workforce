"""Add / edit worker form and the workers list filters."""

from django import forms
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod, PayType
from apps.core.validators import validate_file_size
from apps.worksites.models import Site

from .fields import MoneyField
from .models import Trade, Worker

PHOTO_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%Y-%m-%d")
        super().__init__(**kwargs)


class WorkerForm(forms.ModelForm):
    """Identity, site, pay and inherited settings, on one screen.

    The view hands the cleaned site / rate fields to the services (assignment and rate
    history); the model fields are saved by ``workers.services``.
    """

    photo = forms.FileField(
        label=_("Photo"),
        required=False,
        validators=[FileExtensionValidator(PHOTO_EXTENSIONS), validate_file_size],
        help_text=_(
            "Used by the foreman to recognise the worker at roll call. "
            "JPG, PNG or WebP, 5 MB at most."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )
    new_trade = forms.CharField(
        label=_("Or add a trade"),
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. Tiler")}),
    )
    site = forms.ModelChoiceField(
        label=_("Assign to site"), queryset=Site.objects.none(), required=False
    )
    start_date = forms.DateField(
        label=_("Starting"),
        required=False,
        widget=DateInput(),
        help_text=_("The worker appears on that site’s roll call from the start date."),
    )
    pay_type = forms.ChoiceField(
        label=_("Pay type"), choices=PayType.choices, widget=forms.RadioSelect
    )
    rate = MoneyField(label=_("Rate"))
    effective_from = forms.DateField(label=_("Effective from"), widget=DateInput())
    payment_method = forms.TypedChoiceField(
        label=_("Payment method"),
        choices=[("", _("Use the inherited value")), *PaymentMethod.choices],
        required=False,
        empty_value=None,
    )
    pay_frequency = forms.TypedChoiceField(
        label=_("Pay frequency"),
        choices=[("", _("Use the inherited value")), *PayFrequency.choices],
        required=False,
        empty_value=None,
    )

    class Meta:
        model = Worker
        fields = [
            "first_name",
            "last_name",
            "phone",
            "trade",
            "id_document_type",
            "id_document_number",
            "is_active",
            "payment_method",
            "pay_frequency",
        ]
        widgets = {"phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"})}

    def __init__(self, *args, sites, decimals: int, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today
        self.fields["site"].queryset = sites
        self.fields["rate"].decimals = decimals
        self.fields["trade"].required = False
        self.fields["trade"].empty_label = _("No trade")
        self.fields["id_document_number"].label = _("ID number")
        self.fields["id_document_number"].help_text = _("Optional")
        editing = self.instance.pk is not None
        if not editing:
            del self.fields["is_active"]
            self.fields["site"].required = True
            self.fields["start_date"].required = True
            self.fields["start_date"].initial = today
            self.fields["effective_from"].initial = today
            self.fields["pay_type"].initial = PayType.DAILY
        else:
            self.fields["site"].empty_label = _("No change")
            self.fields["site"].help_text = _("Pick a site to move the worker on the start date.")
            self.fields["effective_from"].initial = today
            self.fields["effective_from"].help_text = _(
                "A new rate applies from this date; earlier rates are kept in the history."
            )
            rate = self.instance.rate_on(today)
            if rate is not None:
                self.fields["pay_type"].initial = rate.pay_type
                self.fields["rate"].initial = rate.amount

    def clean_new_trade(self):
        return " ".join(self.cleaned_data.get("new_trade", "").split())

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("site") is not None and cleaned.get("start_date") is None:
            self.add_error("start_date", _("Set the date the worker starts on this site."))
        return cleaned

    def trade_value(self):
        """The trade to save: an existing one, or a new one typed in."""
        name = self.cleaned_data.get("new_trade")
        if name:
            trade = Trade.objects.filter(name__iexact=name).first()
            return trade or Trade.objects.create(name=name)
        return self.cleaned_data.get("trade")


class WorkerFilterForm(forms.Form):
    STATUS_CHOICES = [("active", _("Active")), ("inactive", _("Inactive")), ("all", _("All"))]

    q = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Name, phone or worker ID")}
        ),
    )
    site = forms.ModelChoiceField(
        label=_("Site"), queryset=Site.objects.none(), required=False, empty_label=_("All sites")
    )
    trade = forms.ModelChoiceField(
        label=_("Trade"), queryset=Trade.objects.all(), required=False, empty_label=_("All trades")
    )
    status = forms.ChoiceField(label=_("Status"), choices=STATUS_CHOICES, required=False)

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites
