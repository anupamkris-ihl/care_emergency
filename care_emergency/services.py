from care.utils.time_util import care_now
from django.db import transaction

from care_emergency.models import MlcSequence


def allocate_mlc_number(facility):
    """Returns the next MLC-<year>-<nnnn> for the facility.

    The counter row stays locked until the request's transaction commits, so
    concurrent registrations get consecutive numbers instead of duplicates.
    """
    year = care_now().year
    with transaction.atomic():
        MlcSequence.objects.get_or_create(facility=facility, year=year)
        sequence = MlcSequence.objects.select_for_update().get(
            facility=facility, year=year
        )
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    return f"MLC-{year}-{sequence.last_value:04d}"
