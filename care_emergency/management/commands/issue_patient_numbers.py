from care.emr.models import Encounter, Patient
from care.facility.models import Facility
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from care_emergency.constants import PatientNumberSeries
from care_emergency.models import EmergencyEncounter, FacilityUnit, PatientRegistration
from care_emergency.services import (
    allocate_patient_number,
    attach_patient_number,
    issue_patient_number,
    patient_number,
)


class Command(BaseCommand):
    help = (
        "Issues patient numbers to existing patients who have none, oldest "
        "first: a UHID, or an UNK number for unidentified emergency patients "
        "not yet converted. Patients that already have one are skipped, so it "
        "is safe to run again."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--default-facility",
            help="Facility id (or external id) for patients with no desk "
            "registration, emergency record or encounter to take it from.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        default = None
        if options["default_facility"]:
            value = options["default_facility"]
            lookup = {"id": value} if value.isdigit() else {"external_id": value}
            default = Facility.objects.filter(**lookup).first()
            if default is None:
                raise CommandError(f"No facility {value}")
        units = set(FacilityUnit.objects.values_list("facility_id", flat=True))
        issued = skipped = 0
        for patient in Patient.objects.order_by("created_date", "id"):
            if patient_number(patient, PatientNumberSeries.uhid) or patient_number(
                patient, PatientNumberSeries.unknown
            ):
                continue
            unknown = (
                EmergencyEncounter.objects.filter(
                    patient=patient, converted_at__isnull=True, is_unidentified=True
                )
                .select_related("facility")
                .order_by("created_date")
                .first()
            )
            facility = unknown.facility if unknown else self.facility_of(patient)
            facility = facility or default
            if facility is None or facility.id not in units:
                skipped += 1
                self.stderr.write(
                    f"Skipped {patient.external_id} ({patient.name}): "
                    + (
                        "no facility"
                        if facility is None
                        else f"{facility} has no unit code"
                    )
                )
                continue
            if options["dry_run"]:
                issued += 1
                self.stdout.write(f"Would number {patient.external_id} at {facility}")
                continue
            with transaction.atomic():
                if unknown:
                    day = timezone.localdate(unknown.created_date)
                    value, _ = allocate_patient_number(
                        PatientNumberSeries.unknown, facility, day
                    )
                    attach_patient_number(patient, PatientNumberSeries.unknown, value)
                else:
                    value = issue_patient_number(patient, facility)
            issued += 1
            self.stdout.write(f"{value}  {patient.name}")
        self.stdout.write(self.style.SUCCESS(f"{issued} numbered, {skipped} skipped"))

    def facility_of(self, patient):
        """Where the patient was registered: the desk, an emergency record, or
        their first encounter."""
        record = PatientRegistration.objects.filter(
            patient=patient, facility__isnull=False
        ).first()
        if record:
            return record.facility
        emergency = (
            EmergencyEncounter.objects.filter(patient=patient)
            .order_by("created_date")
            .first()
        )
        if emergency:
            return emergency.facility
        encounter = (
            Encounter.objects.filter(patient=patient)
            .select_related("facility")
            .order_by("created_date")
            .first()
        )
        return encounter.facility if encounter else None
