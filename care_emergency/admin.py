from django.contrib import admin

from care_emergency.models import (
    AppointmentDetails,
    AppointmentSequence,
    EmergencyDesk,
    EmergencyEncounter,
    FacilityUnit,
    MlcSequence,
    PatientNumberSequence,
    PatientRegistration,
    RegistrationAllergy,
    WalkInReserve,
)


@admin.register(EmergencyDesk)
class EmergencyDeskAdmin(admin.ModelAdmin):
    list_display = ("facility", "phone_number")
    raw_id_fields = ("facility", "created_by", "updated_by")


@admin.register(EmergencyEncounter)
class EmergencyEncounterAdmin(admin.ModelAdmin):
    list_display = (
        "mlc_number",
        "triage",
        "is_medico_legal",
        "is_unidentified",
        "facility",
        "created_date",
    )
    list_filter = ("triage", "is_medico_legal", "is_unidentified")
    search_fields = ("mlc_number", "patient__name", "police_station")
    raw_id_fields = (
        "encounter",
        "patient",
        "facility",
        "created_by",
        "updated_by",
        "converted_by",
    )
    readonly_fields = ("mlc_number",)


@admin.register(MlcSequence)
class MlcSequenceAdmin(admin.ModelAdmin):
    list_display = ("facility", "year", "last_value")
    raw_id_fields = ("facility",)


class RegistrationAllergyInline(admin.TabularInline):
    model = RegistrationAllergy
    extra = 0


@admin.register(PatientRegistration)
class PatientRegistrationAdmin(admin.ModelAdmin):
    list_display = ("patient", "registration_type", "payer_type", "created_date")
    list_filter = ("registration_type", "payer_type", "is_vip")
    search_fields = ("patient__name", "first_name", "last_name")
    raw_id_fields = (
        "patient",
        "facility",
        "vip_flagged_by",
        "created_by",
        "updated_by",
    )
    inlines = [RegistrationAllergyInline]


@admin.register(AppointmentDetails)
class AppointmentDetailsAdmin(admin.ModelAdmin):
    list_display = (
        "appointment_number",
        "consultation_type",
        "visit_purpose",
        "booking_channel",
        "facility",
        "created_date",
    )
    list_filter = ("consultation_type", "visit_purpose", "booking_channel")
    search_fields = ("appointment_number", "booking__patient__name")
    raw_id_fields = ("booking", "facility", "created_by", "updated_by")
    readonly_fields = ("appointment_number",)


@admin.register(AppointmentSequence)
class AppointmentSequenceAdmin(admin.ModelAdmin):
    list_display = ("year", "last_value")


@admin.register(WalkInReserve)
class WalkInReserveAdmin(admin.ModelAdmin):
    list_display = ("availability", "walk_in_every")
    raw_id_fields = ("availability", "created_by", "updated_by")


@admin.register(FacilityUnit)
class FacilityUnitAdmin(admin.ModelAdmin):
    list_display = ("code", "facility")
    raw_id_fields = ("facility", "created_by", "updated_by")


@admin.register(PatientNumberSequence)
class PatientNumberSequenceAdmin(admin.ModelAdmin):
    """Lowering last_value would issue numbers again."""

    list_display = ("series", "facility", "period", "last_value")
    list_filter = ("series",)
    raw_id_fields = ("facility",)
