from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DashboardConfig(AppConfig):
    name = "apps.dashboard"
    verbose_name = _("Dashboard")

    def ready(self):
        from apps.precompute import services as precompute

        from . import rollups

        # The report's rollups are built from pay: after the pay step (order 0).
        precompute.register(rollups.rebuild_step, order=50)
