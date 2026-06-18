from django.apps import AppConfig


class SettingsConfig(AppConfig):
    """
    Business-settings domain app (geofence_settings, customer_portal_settings,
    business_hours, break_settings, reservation_settings, etc. from the old
    schema). Named `settings` after that domain — unrelated to
    config/settings.py (the Django project configuration module). The
    explicit `label` below keeps Django's app registry / migration history
    from being confused by the name collision with the word "settings"
    generally.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "settings"
    label = "business_settings"
    verbose_name = "Business Settings"
