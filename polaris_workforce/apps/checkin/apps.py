from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CheckinConfig(AppConfig):
    name = "apps.checkin"
    verbose_name = _("Check-in")
