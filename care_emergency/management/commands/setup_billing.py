from care.facility.models import Facility
from django.core.management.base import BaseCommand, CommandError

from care_emergency.billing import setup_billing
from care_emergency.models import FacilityUnit


class Command(BaseCommand):
    help = (
        "Loads the front-desk billing masters (heads, v11 catalogue, payer and "
        "concession pricing, coupons, discounts, invoice number) into Care for "
        "a facility, or for every facility with a unit code. Safe to run again: "
        "existing entries are updated in place."
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
            counts = setup_billing(facility)
            self.stdout.write(
                f"{facility.name}: {counts['categories']} categories, "
                f"{counts['definitions']} charge item definitions, {counts['tags']} tags"
            )
