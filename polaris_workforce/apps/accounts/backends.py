"""Office sign-in backend: Django's, plus the company check."""

from django.contrib.auth.backends import ModelBackend


class CompanyModelBackend(ModelBackend):
    """Users of an inactive company cannot sign in, and their open sessions end
    (``get_user`` uses the same check). Platform admins have no company."""

    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):
            return False
        company_id = getattr(user, "company_id", None)
        return company_id is None or user.company.is_active
