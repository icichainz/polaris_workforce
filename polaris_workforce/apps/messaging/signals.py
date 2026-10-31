"""Hooks into payroll: a payment recorded queues the worker's payslip text.

Messaging must never break a payment: the work runs in a savepoint and any error is
logged and swallowed. Nothing is sent here (``send_pending`` does that).
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.payroll.models import PayLine, Payment

from . import services

logger = logging.getLogger("polaris.messaging")


@receiver(post_save, sender=Payment, dispatch_uid="messaging_payslip_on_payment")
def payslip_on_payment(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    try:
        with transaction.atomic():
            services.queue_payslip(instance)
    except Exception:  # noqa: BLE001 - a text must never stop a payment
        logger.exception("could not queue the payslip text for payment %s", instance.pk)


@receiver(post_delete, sender=Payment, dispatch_uid="messaging_payslip_on_payment_undo")
def payslip_on_payment_undo(sender, instance, **kwargs):
    try:
        with transaction.atomic():
            company_id = (
                PayLine.objects.filter(pk=instance.pay_line_id)
                .values_list("period__site__company_id", flat=True)
                .first()
            )
            if company_id is not None:
                services.cancel_payslip(company_id, instance.pk)
    except Exception:  # noqa: BLE001
        logger.exception("could not cancel the payslip text for payment %s", instance.pk)
