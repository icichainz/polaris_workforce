"""Report filters."""

import datetime

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.workers.models import Trade
from apps.worksites.models import Site

MAX_RANGE_DAYS = 366


class ReportFilterForm(forms.Form):
    """Period, site and trade. The site list is limited to the sites the user may see."""

    PERIODS = [
        ("week", _("This week")),
        ("month", _("This month")),
        ("last_month", _("Last month")),
        ("custom", _("Custom…")),
    ]

    period = forms.ChoiceField(
        label=_("Period"), choices=PERIODS, required=False, widget=forms.RadioSelect
    )
    start = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    end = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    site = forms.ModelChoiceField(
        label=_("Site"), queryset=Site.objects.none(), required=False, empty_label=_("All sites")
    )
    trade = forms.ModelChoiceField(
        label=_("Trade"), queryset=Trade.objects.all(), required=False, empty_label=_("All trades")
    )

    def __init__(self, *args, sites, today: datetime.date, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today
        self.fields["site"].queryset = sites

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("period") == "custom":
            start, end = cleaned.get("start"), cleaned.get("end")
            if not start or not end:
                raise forms.ValidationError(_("Pick both dates for a custom period."))
            if end < start:
                self.add_error("end", _("The end date is before the start date."))
            elif (end - start).days >= MAX_RANGE_DAYS:
                self.add_error("end", _("Pick a period of one year or less."))
        return cleaned

    def date_range(self) -> tuple[datetime.date, datetime.date]:
        """The chosen period; this month when the form is unbound or invalid."""
        period = "month"
        data = getattr(self, "cleaned_data", None) if self.is_bound and self.is_valid() else None
        if data:
            period = data.get("period") or "month"
            if period == "custom":
                return data["start"], data["end"]
        return period_dates(period, self.today)

    def selected(self, name):
        if self.is_bound and self.is_valid():
            return self.cleaned_data.get(name)
        return None


def period_dates(period: str, today: datetime.date) -> tuple[datetime.date, datetime.date]:
    if period == "week":
        start = today - datetime.timedelta(days=today.weekday())
        return start, start + datetime.timedelta(days=6)
    first = today.replace(day=1)
    if period == "last_month":
        last_end = first - datetime.timedelta(days=1)
        return last_end.replace(day=1), last_end
    next_month = (first + datetime.timedelta(days=32)).replace(day=1)
    return first, next_month - datetime.timedelta(days=1)
