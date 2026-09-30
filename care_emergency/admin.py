from django.contrib import admin

from care_emergency.models import EmergencyDesk, EmergencyEncounter, MlcSequence


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
