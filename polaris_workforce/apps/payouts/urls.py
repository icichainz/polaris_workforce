"""Bulk-payment exports for bank transfer and mobile money (office pages under /payouts/)."""

from django.urls import path

from . import views

app_name = "payouts"

urlpatterns = [
    path("payouts/", views.batches, name="batches"),
    path("payouts/prepare/", views.prepare, name="prepare"),
    path("payouts/batches/<int:batch_id>/", views.batch, name="batch"),
    path("payouts/batches/<int:batch_id>/export/", views.batch_export, name="batch_export"),
    path("payouts/batches/<int:batch_id>/import/", views.batch_import, name="batch_import"),
    path("payouts/batches/<int:batch_id>/close/", views.batch_close, name="batch_close"),
    path("payouts/batches/<int:batch_id>/cancel/", views.batch_cancel, name="batch_cancel"),
    path("payouts/lines/<int:line_id>/confirm/", views.line_confirm, name="line_confirm"),
    path("payouts/lines/<int:line_id>/fail/", views.line_fail, name="line_fail"),
    path("payouts/lines/<int:line_id>/remove/", views.line_remove, name="line_remove"),
    path("payouts/layouts/", views.layout_list, name="layouts"),
    path("payouts/layouts/new/", views.layout_create, name="layout_create"),
    path("payouts/layouts/<int:layout_id>/", views.layout_edit, name="layout_edit"),
    path("payouts/accounts/", views.accounts, name="accounts"),
    path("payouts/accounts/<int:worker_id>/", views.account_edit, name="account_edit"),
]
