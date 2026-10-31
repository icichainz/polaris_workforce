from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ApiConfig(AppConfig):
    name = "apps.api"
    verbose_name = _("API")

    def ready(self):
        from .schema import quote_times_in_yaml

        quote_times_in_yaml()
