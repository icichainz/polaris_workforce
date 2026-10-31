"""Crew list. Stub: Wave 2 agent B replaces the body (assignment rules live in services)."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework.views import APIView

from apps.api.exceptions import NotImplementedYet
from apps.api.schema import SITE_ID, error_responses

from .serializers import CrewSerializer

TAGS = ["worksites"]


def _worker(worker_id, number, first, last, trade, pay_type="daily", streak=0):
    return {
        "id": worker_id,
        "number": number,
        "first_name": first,
        "last_name": last,
        "trade": trade,
        "photo_url": f"https://polaris.example.com/media/workers/{number}.jpg",
        "pay_type": pay_type,
        "absent_streak": streak,
    }


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
                value={
                    "site_id": 3,
                    "date": "2026-10-31",
                    "count": 3,
                    "workers": [
                        _worker(51, "W-0142", "Komlan", "Agbeko", "Carpenter"),
                        _worker(58, "W-0157", "Kodjo", "Atsu", "Labourer", streak=2),
                        _worker(44, "W-0118", "Yao", "Mensah", "Mason", "hourly"),
                    ],
                },
            )
        ],
    )
    def get(self, request, site_id):
        raise NotImplementedYet()
