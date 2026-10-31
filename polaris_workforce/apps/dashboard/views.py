from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _


@login_required
def home(request):
    return render(request, "dashboard/home.html")


@login_required
def reports(request):
    return render(request, "dashboard/reports.html")


class _GalleryForm(forms.Form):
    """Demo form for the component gallery only."""

    full_name = forms.CharField(label=_("Full name"), initial="Yao Mensah")
    trade = forms.ChoiceField(
        label=_("Trade"),
        choices=[("mason", _("Mason")), ("carpenter", _("Carpenter")), ("labourer", _("Labourer"))],
    )
    daily_rate = forms.IntegerField(label=_("Daily rate"), initial=6000)
    reason = forms.CharField(
        label=_("Correction reason"),
        widget=forms.TextInput(attrs={"placeholder": _("e.g. Bus breakdown, arrived 07:20")}),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        help_text=_("Shown to the foreman on the phone."),
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    keep_signed_in = forms.BooleanField(
        label=_("Keep me signed in on this computer"), required=False, initial=True
    )


def components(request):
    """Component gallery for design review. Only available with DEBUG on."""
    if not settings.DEBUG:
        raise Http404
    form = _GalleryForm(data={"full_name": "Yao Mensah", "trade": "mason", "daily_rate": "6000"})
    form.is_valid()  # populate errors so the error state shows on "reason"
    return render(request, "dashboard/components.html", {"form": form, "unbound": _GalleryForm()})
