from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class WorkersConfig(AppConfig):
    name = "apps.workers"
    verbose_name = _("Workers")
