"""Office forms for presence tracking: zones, gateways, BLE badges, filters, notice."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Gateway, Zone, normalize_badge_id


class ZoneForm(forms.ModelForm):
    """A zone of ``site``. The code is the tracker's zone id: unique on the site."""

    class Meta:
        model = Zone
        fields = ["code", "name"]
        widgets = {
            "code": forms.TextInput(attrs={"placeholder": _("e.g. GATE"), "autocomplete": "off"}),
            "name": forms.TextInput(attrs={"placeholder": _("e.g. Main gate")}),
        }

    def __init__(self, *args, site, **kwargs):
        super().__init__(*args, **kwargs)
        self.site = site
        self.instance.site = site

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        taken = Zone.objects.filter(site=self.site, code=code).exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError(_("This site already has a zone with this code."))
        return code


class GatewayForm(forms.ModelForm):
    """A gateway of ``site``: its label and zone (its token is issued separately)."""

    class Meta:
        model = Gateway
        fields = ["label", "zone"]
        help_texts = {"zone": _("Sightings of this gateway are placed in this zone.")}

    def __init__(self, *args, site, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.site = site
        self.fields["zone"].queryset = Zone.objects.filter(site=site).order_by("code")
        self.fields["zone"].empty_label = _("No zone (the whole site)")
        self.fields["zone"].label_from_instance = lambda zone: f"{zone.code} · {zone.name}"


class BleBadgeForm(forms.Form):
    badge_id = forms.CharField(
        label=_("Badge ID"),
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "placeholder": "ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
        help_text=_(
            "As the badge broadcasts it. For an iBeacon: ib:<uuid>:<major>:<minor>. Case does "
            "not matter: it is saved in lower case."
        ),
    )

    def clean_badge_id(self):
        value = normalize_badge_id(self.cleaned_data["badge_id"])
        if not value:
            raise forms.ValidationError(_("Enter the badge ID."))
        return value


class RevokeForm(forms.Form):
    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. Gateway stolen")}),
    )


class BadgeFilterForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(attrs={"placeholder": _("Name, number or badge ID")}),
    )
    site = forms.ModelChoiceField(label=_("Site"), queryset=None, required=False)

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites
        self.fields["site"].empty_label = _("All my sites")


class DayForm(forms.Form):
    date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))


NOTICE_LANGUAGES = [("en", "English"), ("fr", "Français")]
NOTICE_PAPERS = [("a4", "A4"), ("letter", _("US Letter"))]


class NoticeForm(forms.Form):
    """Options of the printed notice. Invalid values fall back to the defaults."""

    lang = forms.ChoiceField(label=_("Language"), choices=NOTICE_LANGUAGES, required=False)
    paper = forms.ChoiceField(label=_("Paper"), choices=NOTICE_PAPERS, required=False)
    site = forms.ModelChoiceField(label=_("Tracking hours of"), queryset=None, required=False)

    def __init__(self, *args, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = sites
        self.fields["site"].empty_label = _("The company")
