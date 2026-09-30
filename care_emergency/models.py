from care.emr.models.base import EMRBaseModel
from django.db import models
from django.db.models import Q

from care_emergency.constants import TriageChoices


class EmergencyDesk(EMRBaseModel):
    """The facility-owned number used as phone_number for emergency patients."""

    facility = models.OneToOneField(
        "facility.Facility", on_delete=models.PROTECT, related_name="emergency_desk"
    )
    phone_number = models.CharField(max_length=14)


class EmergencyEncounter(EMRBaseModel):
    encounter = models.OneToOneField(
        "emr.Encounter", on_delete=models.PROTECT, related_name="emergency"
    )
    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="emergency_encounters"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    triage = models.CharField(max_length=10, choices=TriageChoices.choices)
    # Nullable so an update can set it back to false (Care drops default values).
    is_medico_legal = models.BooleanField(null=True)
    mlc_nature = models.CharField(max_length=50, blank=True, default="")
    police_station = models.CharField(max_length=200, blank=True, default="")
    mlc_number = models.CharField(max_length=30, null=True, blank=True)
    distinguishing_marks = models.TextField(blank=True, default="")
    brought_by_name = models.CharField(max_length=200, blank=True, default="")
    brought_by_phone = models.CharField(max_length=14, blank=True, default="")
    brought_by_relationship = models.CharField(max_length=50, blank=True, default="")
    is_unidentified = models.BooleanField(default=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "mlc_number"],
                condition=Q(mlc_number__isnull=False),
                name="care_emergency_unique_mlc_number",
            )
        ]


class MlcSequence(models.Model):
    """One counter row per facility per year, incremented under a row lock."""

    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    year = models.PositiveIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "year"], name="care_emergency_unique_mlc_sequence"
            )
        ]

    def __str__(self):
        return f"{self.facility_id}/{self.year}: {self.last_value}"
