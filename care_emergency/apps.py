from django.apps import AppConfig


class CareEmergencyConfig(AppConfig):
    name = "care_emergency"
    verbose_name = "Emergency"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from care_emergency import signals, tasks  # noqa: F401
        from care_emergency.restricted import mask_patient
        from care_emergency.contact import hide_no_phone, patient_phone_allowed

        # Masks restricted patients (D30) wherever Care shows a patient. Care
        # releases without the hook still run the plug, without masking.
        try:
            from care.emr.resources.patient.hooks import (
                patient_serialization_hooks,
                patient_phone_hooks,
            )
        except ImportError:
            return
        if hide_no_phone not in patient_serialization_hooks:
            patient_serialization_hooks.insert(0, hide_no_phone)
        if patient_phone_allowed not in patient_phone_hooks:
            patient_phone_hooks.append(patient_phone_allowed)
        if mask_patient not in patient_serialization_hooks:
            patient_serialization_hooks.append(mask_patient)
