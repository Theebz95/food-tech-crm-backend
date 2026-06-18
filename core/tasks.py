"""
Daily replacement for the old Postgres check_expired_trials() function +
pg_cron job (see supabase/migrations/20260210071436...,
20260218042634...). Same three-step logic as the original, just expressed
as a Celery Beat task against Business instead of profiles.
"""

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Business

ACTIVE_SUBSCRIPTION_STATUSES = ["active", "trialing"]


@shared_task
def check_expired_trials():
    now = timezone.now()
    no_active_subscription = ~Q(subscription_status__in=ACTIVE_SUBSCRIPTION_STATUSES)

    with transaction.atomic():
        # 1. Mark trials expired + deactivate, if past trial_ends_at with no
        #    active/trialing subscription.
        Business.objects.filter(
            trial_expired=False,
            trial_ends_at__isnull=False,
            trial_ends_at__lt=now,
        ).filter(no_active_subscription).update(trial_expired=True, is_active=False)

        # 2. Reactivate any business with an active/trialing subscription.
        Business.objects.filter(
            subscription_status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            is_active=False,
        ).update(is_active=True)

        # 3. Deactivate any non-legacy business whose trial has expired and
        #    still has no active/trialing subscription (covers businesses
        #    that were reactivated and then had their subscription lapse
        #    again).
        Business.objects.filter(
            is_legacy=False,
            trial_expired=True,
            is_active=True,
        ).filter(no_active_subscription).update(is_active=False)
