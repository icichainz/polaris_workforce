"""``manage.py push_tracker_config [--site ID ...]``: push site configs to the tracker now.

Without ``--site``: every site the periodic resync would push (tracked now, tracked at the
last push, or whose last push failed). Useful after starting a tracker by hand.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.tenancy import use_company
from apps.presence import services
from apps.worksites.models import Site


class Command(BaseCommand):
    help = "Push the presence-tracking config of sites to the tracker (TRACKER_URL)."

    def add_arguments(self, parser):
        parser.add_argument("--site", type=int, action="append", help="A site id (repeatable).")

    def handle(self, *args, **options):
        if options["site"]:
            sites = list(Site.objects.filter(pk__in=options["site"]).select_related("company"))
            missing = set(options["site"]) - {site.pk for site in sites}
            if missing:
                raise CommandError(f"Unknown site id(s): {sorted(missing)}")
        else:
            sites = services.sites_to_resync()
        pushed = failed = 0
        for site in sites:
            with use_company(site.company):
                try:
                    if services.push_site_config(site):
                        pushed += 1
                except services.TrackerPushError as exc:
                    failed += 1
                    self.stderr.write(f"Site {site.pk}: {exc}")
        self.stdout.write(f"{pushed} pushed, {failed} failed, {len(sites)} site(s).")
        if failed:
            raise CommandError("Some configs were not pushed.")
