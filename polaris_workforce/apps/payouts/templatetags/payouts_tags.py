"""Template tags for showing transfers on other screens (the cash sheet).

{% load payouts_tags %}
{% transfer_state line as transfer %}           → None for cash, else a TransferState
{% if transfer %}{% transfer_cell transfer %}{% else %}…"Mark paid" form…{% endif %}

``transfer_cell`` renders ``payouts/_transfer_cell.html``: "By mobile money · in PAY-00012"
(linked to the batch), "By bank transfer · not in a batch yet" (linked to preparing one),
or "Transfer failed" with the reason. A line that is paid shows the cash sheet's usual
"Paid" badge instead; the tag is for lines still to pay.
"""

from django import template

from .. import services

register = template.Library()


@register.simple_tag
def transfer_state(pay_line, company=None):
    """``None`` when ``pay_line`` is paid in cash, else its ``TransferState``."""
    return services.transfer_state(pay_line, company)


@register.inclusion_tag("payouts/_transfer_cell.html")
def transfer_cell(state, pay_date=None, site=None):
    return {"state": state, "pay_date": pay_date, "site": site}
