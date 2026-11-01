"""Polaris Workforce project configuration.

The Celery app is loaded with Django, so ``@shared_task`` tasks use it.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
