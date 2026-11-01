from django.apps import AppConfig
from django.contrib.staticfiles.apps import StaticFilesConfig as BaseStaticFilesConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    # The default for "apps.core": this module holds a second config (StaticFilesConfig).
    default = True
    name = "apps.core"
    verbose_name = _("Core")


class StaticFilesConfig(BaseStaticFilesConfig):
    """``django.contrib.staticfiles``, without the Tailwind source (``static/src/``).

    Only the built ``static/css/app.css`` is served (scripts/build_css.sh). The source is
    not CSS a browser can use, and its ``@import "tailwindcss"`` would make collectstatic
    fail while hashing file names.
    """

    ignore_patterns = [*BaseStaticFilesConfig.ignore_patterns, "src"]
