"""Form fields shared by the workers and worksites forms."""

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.money import from_minor, to_minor

# Spaces people type as thousands separators ("6 000", narrow no-break space…).
_SPACES = str.maketrans("", "", "    ")


class MoneyField(forms.CharField):
    """A major-unit amount typed by a person, cleaned to an int of minor units.

    ``decimals`` is the currency's number of decimal places (``CompanySettings``).
    Accepts "6000", "6 000", "1234.50" and "1234,50"; never goes through a float.
    """

    default_error_messages = {
        "invalid": _("Enter an amount, for example 6000."),
        "too_precise": _("Use at most %(decimals)s decimal place(s)."),
        "negative": _("The amount cannot be negative."),
    }

    def __init__(self, *, decimals: int = 0, **kwargs):
        self.decimals = decimals
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal"}))
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, int) and not isinstance(value, bool):
            amount = from_minor(value, self.decimals)
            return f"{amount:f}"
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        text = value.translate(_SPACES).replace(",", ".")
        try:
            Decimal(text)
        except ArithmeticError:
            raise forms.ValidationError(self.error_messages["invalid"], code="invalid") from None
        try:
            amount = to_minor(text, self.decimals)
        except ValueError:
            if not text.replace(".", "", 1).lstrip("-").isdigit():
                raise forms.ValidationError(
                    self.error_messages["invalid"], code="invalid"
                ) from None
            raise forms.ValidationError(
                self.error_messages["too_precise"],
                code="too_precise",
                params={"decimals": self.decimals},
            ) from None
        if amount < 0:
            raise forms.ValidationError(self.error_messages["negative"], code="negative")
        return amount
