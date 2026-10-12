from django.apps import AppConfig


class CareEmergencyConfig(AppConfig):
    name = "care_emergency"
    verbose_name = "Emergency"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from care_emergency import signals, tasks  # noqa: F401
