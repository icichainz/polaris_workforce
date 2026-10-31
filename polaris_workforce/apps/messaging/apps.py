from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MessagingConfig(AppConfig):
    name = "apps.messaging"
    verbose_name = _("Messaging")

    def ready(self):
        from . import signals  # noqa: F401 (connects the payroll hooks)
