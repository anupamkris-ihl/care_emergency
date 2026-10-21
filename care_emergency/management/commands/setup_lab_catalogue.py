from care.facility.models import Facility
from django.core.management.base import BaseCommand, CommandError

from care_emergency.lab_catalogue import seed_lab_catalogue
from care_emergency.models import FacilityUnit


class Command(BaseCommand):
    help = (
        "Adds the starting lab test catalogue (activity, specimen and observation "
        "definitions, linked to the v11 tariff) for a facility, or for every facility "
        "with a unit code. Run setup_billing first. Safe to run again: tests that "
        "already exist are left as the admin set them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--facility", help="Facility id or external id")

    def handle(self, *args, **options):
        if options["facility"]:
            value = options["facility"]
            lookup = {"id": value} if value.isdigit() else {"external_id": value}
            facilities = list(Facility.objects.filter(**lookup))
            if not facilities:
                raise CommandError(f"No facility {value}")
        else:
            facilities = [
                unit.facility
                for unit in FacilityUnit.objects.select_related("facility")
            ]
            if not facilities:
                raise CommandError("No facility has a unit code yet")
        for facility in facilities:
            added = seed_lab_catalogue(facility)
            self.stdout.write(f"{facility.name}: {added} lab tests added")
