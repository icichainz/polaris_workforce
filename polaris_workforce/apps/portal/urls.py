"""Worker portal (``/portal/``) and its office screens (``/portal/office/``).

Worker pages use the portal session (phone + PIN), never the office sign-in; the office
screens use the office sign-in (admins and site managers).
"""

from django.urls import path

from . import office_views, views

app_name = "portal"

urlpatterns = [
    # Workers (portal session)
    path("", views.home, name="home"),
    path("sign-in/", views.sign_in, name="sign_in"),
    path("sign-out/", views.sign_out, name="sign_out"),
    path("days/", views.days, name="days"),
    path("pay/", views.pay, name="pay"),
    path("pay/<int:line_id>/payslip/", views.payslip, name="payslip"),
    path("advances/", views.advances, name="advances"),
    path("pin/", views.change_pin, name="change_pin"),
    # Office (admins, site managers)
    path("office/", office_views.access_list, name="office_access"),
    path("office/workers/<int:worker_id>/", office_views.worker_access, name="office_worker"),
    path("office/workers/<int:worker_id>/pin/", office_views.set_pin, name="office_set_pin"),
    path("office/workers/<int:worker_id>/revoke/", office_views.revoke, name="office_revoke"),
]
