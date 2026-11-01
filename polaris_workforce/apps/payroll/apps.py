from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PayrollConfig(AppConfig):
    name = "apps.payroll"
    verbose_name = _("Payroll")

    def ready(self):
        from apps.precompute import services as precompute

        from . import services

        # Pay is the first figure a site's rebuild recomputes; snapshots read it after.
        precompute.register(services.refresh_pay_step, order=0)
