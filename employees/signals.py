"""
Reacts to BusinessMembership deactivation, regardless of how it happens
(Django admin, a future API endpoint, a one-off script) — every path goes
through BusinessMembership.save(), which is what post_save fires on.

Lives here (not core/signals.py) on purpose: core.models.BusinessMembership
shouldn't need to know employees exists. employees is the domain that
cares about this side effect, so it's the one that connects to core's
model — the standard Django pattern for one app reacting to another app's
model, and it keeps this entirely decoupled from core.

pre_save stashes the previous is_active value on the instance so post_save
can detect a real True->False *transition* — not just "is currently
False," which would re-run the cascade (harmlessly, since it's idempotent,
but pointlessly) on every subsequent save of an already-deactivated row.
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from core.models import BusinessMembership

from . import services


@receiver(pre_save, sender=BusinessMembership)
def _stash_previous_is_active(sender, instance, **kwargs):
    if instance.pk:
        instance._previous_is_active = sender.objects.filter(pk=instance.pk).values_list(
            "is_active", flat=True
        ).first()
    else:
        instance._previous_is_active = None


@receiver(post_save, sender=BusinessMembership)
def _handle_deactivation(sender, instance, created, **kwargs):
    if created:
        return
    previous = getattr(instance, "_previous_is_active", None)
    if previous is True and instance.is_active is False:
        services.handle_membership_deactivation(instance)
