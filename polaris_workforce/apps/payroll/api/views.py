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
from apps.api import examples as ex
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
            "`cash_in_hand = cash_issued − paid_total`.\n\n"
            "**`pay_line_id`** is stable for a worker and pay period while the line is "
            "recomputed (a correction, an approval): the line is updated in place. It "
            "changes only if the worker drops off the sheet (no worked day left in the "
            "period, e.g. corrected to absent) and comes back later, which makes a new "
            "line. A paid line is frozen. Pay from the latest sheet: a queued payment "
            "for a line that no longer exists gets `404 not_found`, and one whose `net` "
            "changed gets `400` on `amount`; fetch the sheet again."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: PayoutSerializer, **error_responses(401, 403, 404)},
        examples=[
            OpenApiExample(
                "Open sheet",
                response_only=True,
                value=ex.PAYOUT,
            )
        ],
    )
    def get(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        sheet = services.read_cash_sheet(site, date)
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
            "period locked: `423 locked`.\n\n"
            "Proof rules come from `GET /me/` → `company.payments`: `proof_type` must be "
            "in `accepted_proofs`, and a type in `file_required_for` (`photo`) needs "
            "`proof_file`. `amount` must equal what is left to pay on the line (its `net` "
            "when unpaid). A `pay_line_id` that no longer exists: `404 not_found`."
        ),
        request={
            "multipart/form-data": PaymentCreateSerializer,
            "application/json": PaymentCreateSerializer,
        },
        responses={
            201: PaymentSerializer,
            200: PaymentSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                423,
                examples={
                    400: [
                        (
                            "Photo missing",
                            "Invalid input.",
                            {"proof_file": ["Attach the photo of the worker receiving the cash."]},
                        ),
                        (
                            "Wrong amount",
                            "Invalid input.",
                            {"amount": ["The amount must be the net left to pay: 4000."]},
                        ),
                    ],
                    409: [
                        (
                            "Already paid",
                            "This worker has already been paid.",
                            {"pay_line_id": 801},
                        ),
                        (
                            "Sheet not open",
                            "The cash sheet is not open: the roll call is not approved yet.",
                            {"status": "not_open"},
                        ),
                    ],
                    423: [("Period locked", "This pay period is locked.", {"status": "locked"})],
                },
            ),
        },
        examples=[
            OpenApiExample(
                "Pay with photo proof",
                request_only=True,
                value={
                    "client_uuid": ex.PAYMENT["client_uuid"],
                    "pay_line_id": ex.PAYMENT["pay_line_id"],
                    "amount": ex.PAYMENT["amount"],
                    "proof_type": ex.PAYMENT["proof_type"],
                    "paid_at": ex.PAYMENT["paid_at"],
                },
            ),
            OpenApiExample(
                "Paid",
                response_only=True,
                status_codes=["201", "200"],
                value=ex.PAYMENT,
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
