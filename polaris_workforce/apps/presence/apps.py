from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PresenceConfig(AppConfig):
    name = "apps.presence"
    verbose_name = _("Presence tracking")

    def ready(self):
        from . import signals  # noqa: F401 (pushes site configs after tracking changes)
