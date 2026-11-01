"""``manage.py reconcile_presence [--site ID ...] [--date YYYY-MM-DD]``: correct past days'
presence sessions with the tracker's replay now (``apps.presence.reconcile``).

Without ``--date``: what the nightly run does, every company's past days not reconciled
yet (failed nights included). With ``--date``: that day (company time zone) again, even
if it was reconciled, once it is over. ``--site`` limits the run to some sites.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.presence import reconcile
from apps.worksites.models import Site


class Command(BaseCommand):
    help = "Correct presence sessions of past days with the tracker's replay (TRACKER_URL)."

    def add_arguments(self, parser):
        parser.add_argument("--site", type=int, action="append", help="A site id (repeatable).")
        parser.add_argument(
            "--date",
            type=datetime.date.fromisoformat,
            help="The local day, YYYY-MM-DD (default: every past day not reconciled yet).",
        )

    def handle(self, *args, **options):
        site_ids = options["site"]
        if site_ids:
            found = set(Site.objects.filter(pk__in=site_ids).values_list("pk", flat=True))
            missing = set(site_ids) - found
            if missing:
                raise CommandError(f"Unknown site id(s): {sorted(missing)}")
        summary = reconcile.run(site_ids=site_ids, day=options["date"])
        if not summary.configured:
            raise CommandError("TRACKER_URL is not set: there is no tracker to ask.")
        for site_id, why in summary.skipped:
            self.stdout.write(f"Site {site_id}: skipped ({why}).")
        for outcome in summary.outcomes:
            if outcome.ok:
                self.stdout.write(
                    f"Site {outcome.site_id} on {outcome.date}: {outcome.added} added, "
                    f"{outcome.changed} changed, {outcome.removed} removed, "
                    f"{outcome.unchanged} unchanged, {outcome.ignored} ignored."
                )
            else:
                self.stderr.write(f"Site {outcome.site_id} on {outcome.date}: {outcome.error}")
        self.stdout.write(
            f"{summary.reconciled} day(s) reconciled, {summary.failed} failed, "
            f"{len(summary.skipped)} site(s) skipped."
        )
        if summary.failed:
            raise CommandError("Some days were not reconciled; they are tried again tonight.")
