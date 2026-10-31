"""Office forms for check-in: filters, geofence, kiosk, badge revoke."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import MAX_RADIUS_M, MIN_RADIUS_M


class BadgeFilterForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(attrs={"placeholder": _("Name or number")}),
    )
    site = forms.ModelChoiceField(label=_("Site"), queryset=None, required=False)

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites
        self.fields["site"].empty_label = _("All my sites")


class DayPickForm(forms.Form):
    site = forms.ModelChoiceField(label=_("Site"), queryset=None, empty_label=None)
    date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites


class GeofenceForm(forms.Form):
    latitude = forms.DecimalField(
        label=_("Latitude"),
        max_digits=9,
        decimal_places=6,
        min_value=-90,
        max_value=90,
        help_text=_("Decimal degrees, e.g. 6.131944 (copy it from a map)."),
    )
    longitude = forms.DecimalField(
        label=_("Longitude"),
        max_digits=9,
        decimal_places=6,
        min_value=-180,
        max_value=180,
        help_text=_("Decimal degrees, e.g. 1.222778."),
    )
    radius_m = forms.IntegerField(
        label=_("Radius (metres)"),
        min_value=MIN_RADIUS_M,
        max_value=MAX_RADIUS_M,
        initial=150,
        help_text=_("Check-ins with GPS farther than this from the point are flagged."),
    )


class KioskForm(forms.Form):
    site = forms.ModelChoiceField(label=_("Kiosk for site"), queryset=None, empty_label=None)

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites


class RevokeForm(forms.Form):
    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. Lost badge")}),
    )
