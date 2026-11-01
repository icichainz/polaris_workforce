from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PrecomputeConfig(AppConfig):
    name = "apps.precompute"
    verbose_name = _("Precomputed figures")

    def ready(self):
        from . import signals  # noqa: F401 (marks what each audit entry makes out of date)
