"""How a batch reaches the bank or mobile-money operator, and how confirmations come back.

Today every batch uses the ``FileProvider``: the office downloads a bulk-payment file in
the batch's export layout, uploads it to the bank's or operator's business portal, and
later imports the portal's confirmation report (or confirms lines by hand).

Plugging in an API provider later
---------------------------------
Write a subclass of ``PayoutProvider`` and register it in ``PROVIDERS``:

* ``submit(batch, lines, layout, company_name)`` sends the transfers. A file provider
  returns the ``ExportFile`` to download; an API provider calls the provider's bulk
  payment endpoint (one idempotency key per ``PayoutLine.reference``, so a retry never
  pays twice) and returns ``None`` or a receipt. ``services.export_batch`` moves the
  batch to "exported" and writes the audit entry either way.
* ``confirmations(batch, source)`` returns ``ConfirmationRow``s: parsed from an uploaded
  file here; fetched from the provider's status endpoint, or built from a webhook body,
  for an API provider. ``services.apply_confirmations`` then matches the rows to the
  batch lines and records the payments exactly as for a file, so idempotency, the audit
  trail and the "failed lines go back to pay" rule are shared.
* Credentials belong in the environment (``config/settings.py``), never in the
  database; the batch keeps only the provider key (``PayoutBatch.provider``).
* A management command or a webhook view can call ``apply_confirmations`` without a
  request: run it inside ``use_company(batch.company)`` with a system user that may pay
  out on the batch's sites.
"""

from __future__ import annotations

from .confirmations import ConfirmationRow, parse
from .layouts import ExportFile, build_file


class PayoutProvider:
    key = ""
    label = ""
    #: True when ``submit`` returns a file for the office to download.
    produces_file = False

    def submit(self, batch, lines, layout, company_name) -> ExportFile | None:
        raise NotImplementedError

    def confirmations(self, batch, source) -> list[ConfirmationRow]:
        raise NotImplementedError


class FileProvider(PayoutProvider):
    """Bulk-payment files uploaded by hand to the provider's portal."""

    key = "file"
    label = "File export"
    produces_file = True

    def submit(self, batch, lines, layout, company_name) -> ExportFile:
        return build_file(batch, lines, layout, company_name)

    def confirmations(self, batch, source) -> list[ConfirmationRow]:
        """``source``: ``(file name, bytes)`` of the uploaded confirmation report."""
        name, content = source
        return parse(name, content, decimals=batch.currency_decimals, layout=batch.layout)


PROVIDERS: dict[str, PayoutProvider] = {FileProvider.key: FileProvider()}


def get_provider(key: str) -> PayoutProvider:
    return PROVIDERS[key or FileProvider.key]
