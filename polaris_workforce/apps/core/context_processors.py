"""Template context shared by every office page."""

from django.utils.functional import cached_property


class NavBadges:
    """The counts shown in the sidebar (``nav_badges.approvals``).

    Each count is a single query, run only when a template reads it (responses without
    the sidebar, such as htmx partials, never pay for it) and at most once per request.
    Anonymous visitors get 0 without a query.
    """

    def __init__(self, user):
        self._user = user

    @cached_property
    def approvals(self) -> int:
        """Submitted roll calls waiting for approval on the user's sites."""
        if self._user is None or not self._user.is_authenticated:
            return 0
        # Imported here: settings load this module before the apps are ready.
        from apps.attendance.selectors import days_waiting_for_approval

        return days_waiting_for_approval(self._user).order_by().count()


def nav_badges(request) -> dict:
    return {"nav_badges": NavBadges(getattr(request, "user", None))}
