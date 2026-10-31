"""Cash payout over the API: the day's cash sheet and payments with proof.

Thin views: the figures come from ``apps.payroll.services`` (and its engine); site
scoping goes through ``apps.accounts.permissions.get_site_or_403``.
"""

from django.http import Http404
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import get_site_or_403
from apps.api.idempotency import IdempotentCreateMixin, run_once
from apps.api.schema import DATE, SITE_ID, error_responses

from .. import services
from ..models import PayLine, Payment
from .serializers import (
    PaymentCreateSerializer,
    PaymentSerializer,
    PayoutSerializer,
    payment_data,
    payout_data,
)

TAGS = ["payroll"]

XOF = {"code": "XOF", "decimals": 0}


def _line(line_id, worker_id, name, trade, worked, units, gross, advance, payment=None):
    return {
        "pay_line_id": line_id,
        "worker": {
            "id": worker_id,
            "name": name,
            "trade": trade,
            "photo_url": f"https://polaris.example.com/media/workers/{worker_id}.jpg",
        },
        "worked": worked,
        "units": units,
        "gross": gross,
        "advance_deduction": advance,
        "net": gross - advance,
        "payment": payment,
    }


PAYMENT = {
    "id": 3301,
    "client_uuid": "e4b1c9d2-6a3f-4d8e-b7c0-1a2b3c4d5e61",
    "amount": 4000,
    "proof_type": "photo",
    "paid_at": "2026-10-31T16:31:00Z",
}


class PayoutView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_payout",
        summary="Cash sheet for the day",
        description=(
            "Who to pay and how much, with the cash reconciliation. Before the roll call "
            "is approved `status` is `not_open` and the figures are a preview "
            "('Preview tonight's payout'). Every money value is an integer in the "
            "smallest unit of `currency`.\n\n"
            "Invariants: `total_to_pay = Σ net`, `left_to_pay = total_to_pay − paid_total`, "
            "`cash_in_hand = cash_issued − paid_total`."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: PayoutSerializer, **error_responses(401, 403, 404)},
        examples=[
            OpenApiExample(
                "Open sheet",
                response_only=True,
                value={
                    "site_id": 3,
                    "date": "2026-10-31",
                    "status": "open",
                    "day_status": "approved",
                    "approved_at": "2026-10-31T16:10:00Z",
                    "currency": XOF,
                    "cash_issued": 20000,
                    "total_to_pay": 13250,
                    "paid_total": 4000,
                    "left_to_pay": 9250,
                    "cash_in_hand": 16000,
                    "line_count": 3,
                    "paid_count": 1,
                    "lines": [
                        _line(801, 44, "Yao Mensah", "Mason", "1 day", "1.00", 6000, 2000, PAYMENT),
                        _line(802, 51, "Afi Dosseh", "Labourer", "½ day", "0.50", 1750, 0),
                        _line(803, 63, "Delali Agbo", "Painter", "1 day · late", "1.00", 7500, 0),
                    ],
                },
            )
        ],
    )
    def get(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        sheet = services.cash_sheet(site, date, actor=request.user)
        return Response(PayoutSerializer(payout_data(sheet, request)).data)


class PaymentCreateView(IdempotentCreateMixin, APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        tags=TAGS,
        operation_id="payment_create",
        summary="Record a payment with proof",
        description=(
            "Records that a worker received their pay. Send `multipart/form-data` when "
            "there is a proof file, JSON otherwise.\n\n"
            "201 when created; repeating the same `client_uuid` returns the original "
            "payment with 200 and `Idempotent-Replayed: true` (send the file again on a "
            "retry; it is ignored on a replay). The line already paid under another "
            "`client_uuid`, or the sheet `not_open`: `409 conflict`. Sheet closed or "
            "period locked: `423 locked`."
        ),
        request={
            "multipart/form-data": PaymentCreateSerializer,
            "application/json": PaymentCreateSerializer,
        },
        responses={
            201: PaymentSerializer,
            200: PaymentSerializer,
            **error_responses(400, 401, 403, 404, 409, 423),
        },
        examples=[
            OpenApiExample(
                "Pay with photo proof",
                request_only=True,
                value={
                    "client_uuid": PAYMENT["client_uuid"],
                    "pay_line_id": 801,
                    "amount": 4000,
                    "proof_type": "photo",
                    "paid_at": "2026-10-31T16:31:00Z",
                },
            ),
            OpenApiExample(
                "Paid",
                response_only=True,
                status_codes=["201", "200"],
                value={
                    **PAYMENT,
                    "pay_line_id": 801,
                    "currency": XOF,
                    "proof_url": "https://polaris.example.com/media/proofs/3301.jpg",
                    "paid_by": {"id": 7, "name": "Kossi Amegah"},
                },
            ),
        ],
    )
    def post(self, request):
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        line = PayLine.objects.select_related("period").filter(pk=data["pay_line_id"]).first()
        if line is None:
            raise Http404
        get_site_or_403(request.user, line.period.site_id)
        result = run_once(
            Payment.objects,
            data["client_uuid"],
            create=lambda: services.record_payment(
                request.user,
                line,
                amount=data["amount"],
                proof_type=data["proof_type"],
                proof_file=data.get("proof_file"),
                paid_at=data.get("paid_at"),
                client_uuid=data["client_uuid"],
            ),
            matches=lambda p: p.pay_line_id == line.pk and p.paid_by_id == request.user.pk,
        )
        return self.idempotent_response(
            result, PaymentSerializer(payment_data(result.instance, request)).data
        )
