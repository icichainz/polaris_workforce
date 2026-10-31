"""Crew list for the phone roll call (assignment rules live in ``worksites.services``)."""

from django.utils.translation import gettext
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import get_site_or_403
from apps.api import examples as ex
from apps.api.schema import SITE_ID, error_responses
from apps.core import files
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.workers import services as worker_services

from .. import services
from .serializers import CrewQuerySerializer, CrewSerializer

TAGS = ["worksites"]


def _api_pay_type(value: str) -> str:
    """The contract's pay type: the phone knows ``daily`` and ``hourly`` only (1.2).

    A piece-rate worker is marked like a daily one (present or not); their pay comes from
    the piece-work entries made in the office.
    """
    return "hourly" if value == "hourly" else "daily"


class CrewView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_crew",
        summary="Workers expected on a site that day",
        description=(
            "Every active worker assigned to the site on `date` (assignment start ≤ date ≤ "
            "end), sorted by last name, with the pay type in effect that day. The whole "
            "crew in one response (not paginated): the phone caches it, with the photos, "
            "for offline roll calls.\n\n"
            "`403 permission_denied` if the foreman is not assigned to the site."
        ),
        parameters=[
            SITE_ID,
            OpenApiParameter(
                "date",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                required=False,
                description="Work day (YYYY-MM-DD). Default: today in the company time zone.",
            ),
        ],
        responses={200: CrewSerializer, **error_responses(400, 401, 403, 404)},
        examples=[
            OpenApiExample(
                "Crew",
                response_only=True,
                value=ex.CREW,
            )
        ],
    )
    def get(self, request, site_id):
        site = get_site_or_403(request.user, site_id)
        query = CrewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        company = CompanySettings.load()
        date = query.validated_data.get("date") or services.company_today(company)

        crew = list(services.crew_for(site, date))
        rates = worker_services.rates_on(crew, date)
        streaks = worker_services.absent_streaks(crew, date)
        default_pay_type = resolve_setting("default_pay_type", company=company)
        workers = [
            {
                "id": worker.pk,
                "number": worker.number,
                "first_name": worker.first_name,
                "last_name": worker.last_name,
                "trade": gettext(worker.trade.name) if worker.trade else None,
                "photo_url": files.api_worker_photo_url(request, worker),
                "pay_type": _api_pay_type(
                    rates[worker.pk].pay_type if worker.pk in rates else default_pay_type
                ),
                "absent_streak": streaks[worker.pk],
            }
            for worker in crew
        ]
        data = {"site_id": site.pk, "date": date, "count": len(workers), "workers": workers}
        return Response(CrewSerializer(data).data)
