"""Put the data of a single-company installation into a default company.

Runs between adding the nullable ``company`` columns and making them required. On a
database with data, it creates one company — named after ``CompanySettings.company_name``
(else "Default company") — attaches the existing settings row to it (or creates one), and
gives it every user (superusers included: they become admins of that company; create a
platform admin with ``createsuperuser`` afterwards), site, trade, worker and audit entry.
An empty database (a fresh install, the test database) gets no company.
"""

from django.db import migrations
from django.utils.text import slugify

DEFAULT_NAME = "Default company"


def _unique_slug(Company, name):
    base = slugify(name)[:50] or "company"
    slug, n = base, 1
    while Company.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def forwards(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    CompanySettings = apps.get_model("core", "CompanySettings")
    AuditLog = apps.get_model("core", "AuditLog")
    User = apps.get_model("accounts", "User")
    Site = apps.get_model("worksites", "Site")
    Trade = apps.get_model("workers", "Trade")
    Worker = apps.get_model("workers", "Worker")
    owned = (User, Site, Trade, Worker, AuditLog)

    settings_row = CompanySettings.objects.filter(company__isnull=True).order_by("pk").first()
    if settings_row is None and not any(
        model.objects.filter(company__isnull=True).exists() for model in owned
    ):
        return  # nothing from before companies existed

    name = ((settings_row.company_name if settings_row else "") or "").strip() or DEFAULT_NAME
    company = Company.objects.create(name=name, slug=_unique_slug(Company, name))
    if settings_row is not None:
        settings_row.company = company
        settings_row.save(update_fields=["company"])
        # Any other row (there should be none: settings were a singleton) is dropped.
        CompanySettings.objects.filter(company__isnull=True).delete()
    else:
        CompanySettings.objects.create(company=company, company_name=name)
    for model in owned:
        model.objects.filter(company__isnull=True).update(company=company)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_company"),
        ("accounts", "0006_user_company"),
        ("worksites", "0003_site_company"),
        ("workers", "0002_company"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
