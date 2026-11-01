"""Rebuild the figures of every site something made out of date, for every active company.

Cron-friendly: it only rebuilds marked sites (and each active site once a new day starts),
so running it often costs little. Every few minutes, and a full sweep at night::

    */5 * * * *  cd /srv/polaris/polaris_workforce && .venv/bin/python manage.py \\
                 refresh_precomputed
    30 0 * * *   cd /srv/polaris/polaris_workforce && .venv/bin/python manage.py \\
                 refresh_precomputed --sweep
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Company
from apps.core.tenancy import use_company
from apps.precompute import services


class Command(BaseCommand):
    help = "Rebuild the precomputed figures of the sites that are out of date."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", action="append", default=[], help="Only this company (its short name)."
        )
        parser.add_argument(
            "--sweep",
            action="store_true",
            help="Also rebuild every open pay period, and report what changed unmarked.",
        )

    def handle(self, *args, **options):
        companies = Company.objects.filter(is_active=True).order_by("pk")
        if options["company"]:
            companies = companies.filter(slug__in=options["company"])
            if not companies.exists():
                raise CommandError("No active company with that short name.")
        for company in companies:
            with use_company(company):
                run = services.run(company.pk, sweep=options["sweep"])
            line = f"{company.slug}: {run.sites} site(s) rebuilt"
            if run.mismatches:
                line += f", {len(run.mismatches)} changed without a mark"
            self.stdout.write(line)
