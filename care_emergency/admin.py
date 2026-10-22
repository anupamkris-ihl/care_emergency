from django.contrib import admin

from care_emergency.models import (
    ApiKey,
    AppointmentDetails,
    AppointmentSequence,
    ArtCouple,
    BreakGlassAccess,
    EmergencyDesk,
    EmergencyEncounter,
    FacilityUnit,
    MlcSequence,
    NewbornBirth,
    PatientDocument,
    PatientNumberSequence,
    PatientRegistration,
    RegistrationAllergy,
    RestrictedPatient,
    StatutoryFlag,
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
        "chief_complaint",
        "is_medico_legal",
        "is_unidentified",
        "facility",
        "created_date",
    )
    list_filter = (
        "triage",
        "chief_complaint",
        "mode_of_arrival",
        "is_medico_legal",
        "is_unidentified",
    )
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


@admin.register(PatientDocument)
class PatientDocumentAdmin(admin.ModelAdmin):
    list_display = ("category", "side", "capture_method", "patient", "created_date")
    list_filter = ("category", "capture_method")
    raw_id_fields = ("file", "patient", "created_by", "updated_by")


@admin.register(ArtCouple)
class ArtCoupleAdmin(admin.ModelAdmin):
    list_display = ("woman", "partner", "single_woman", "art_consent", "created_date")
    raw_id_fields = ("woman", "partner", "facility", "created_by", "updated_by")


@admin.register(NewbornBirth)
class NewbornBirthAdmin(admin.ModelAdmin):
    list_display = ("baby", "mother", "born_at", "birth_order", "created_date")
    raw_id_fields = ("baby", "mother", "facility", "created_by", "updated_by")


@admin.register(RestrictedPatient)
class RestrictedPatientAdmin(admin.ModelAdmin):
    """The alias only; the patient's name would defeat the masking."""

    list_display = ("alias", "created_date")
    search_fields = ("alias",)
    raw_id_fields = ("patient",)


@admin.register(StatutoryFlag)
class StatutoryFlagAdmin(admin.ModelAdmin):
    list_display = ("patient_id", "category", "created_by", "created_date")
    list_filter = ("category",)
    raw_id_fields = ("patient", "encounter", "created_by", "updated_by")


@admin.register(BreakGlassAccess)
class BreakGlassAccessAdmin(admin.ModelAdmin):
    """The audit trail: read-only."""

    list_display = ("user", "patient_id", "reason", "created_date", "expires_at")
    raw_id_fields = ("patient", "user")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    """Keys are created in Settings → API keys, which shows the key once."""

    list_display = ("name", "prefix", "user", "expires_at", "revoked_at", "last_used_at")
    search_fields = ("name", "prefix", "user__username")
    raw_id_fields = ("user", "created_by", "updated_by")
    readonly_fields = ("prefix", "key_hash", "last_used_at")

    def has_add_permission(self, request):
        return False
