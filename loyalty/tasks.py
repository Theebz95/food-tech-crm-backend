from celery import shared_task

from .services import expire_due_points


@shared_task
def expire_points():
    """
    Daily Celery Beat task — the actual fix for the Phase 1 audit finding
    that no expiration enforcement existed on points at all. Only
    businesses whose LoyaltyProgram sets points_expire_after_days are
    affected; the default (null) means points never expire, and this
    task is a no-op for those. See loyalty/services.py:expire_due_points
    and PointsTransaction's docstring for exactly how expiration works.
    """
    return expire_due_points()
