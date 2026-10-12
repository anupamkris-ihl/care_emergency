import datetime

from care.emr.api.viewsets.scheduling.availability import (
    convert_availability_and_exceptions_to_slots,
)
from care.emr.models import AvailabilityException, TokenBooking
from care.emr.models.patient import PatientIdentifier, PatientIdentifierConfig
from care.emr.resources.patient_identifier.spec import (
    IdentifierConfig,
    PatientIdentifierStatus,
    PatientIdentifierUse,
)
from care.emr.resources.scheduling.slot.spec import CANCELLED_STATUS_CHOICES
from care.utils.time_util import care_now
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from care_emergency.constants import (
    PATIENT_NUMBER_SYSTEMS,
    BookingChannels,
    PatientNumberSeries,
    VisitPurposes,
)
from care_emergency.models import (
    AppointmentSequence,
    FacilityUnit,
    MlcSequence,
    PatientNumberSequence,
    WalkInReserve,
)


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


def allocate_appointment_number():
    """Returns the next APT-<yy>-<nnnnnn>, locked the same way as MLC numbers."""
    year = care_now().year
    with transaction.atomic():
        AppointmentSequence.objects.get_or_create(year=year)
        sequence = AppointmentSequence.objects.select_for_update().get(year=year)
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    return f"APT-{year % 100:02d}-{sequence.last_value:06d}"


def unit_code(facility):
    unit = FacilityUnit.objects.filter(facility=facility).first()
    if unit is None:
        raise ValidationError("Set the unit code for this facility first")
    return unit.code


def patient_number_config(series):
    """The instance-wide Care identifier config holding the series, created on
    first use. auto_maintained stops staff editing the values through Care."""
    series = PatientNumberSeries(series)
    system = PATIENT_NUMBER_SYSTEMS[series]
    config = PatientIdentifierConfig.objects.filter(
        config__system=system, facility__isnull=True
    ).first()
    if config is None:
        config = PatientIdentifierConfig.objects.create(
            status=PatientIdentifierStatus.active.value,
            config=IdentifierConfig(
                use=PatientIdentifierUse.official
                if series == PatientNumberSeries.uhid
                else PatientIdentifierUse.temp,
                system=system,
                required=False,
                unique=True,
                regex="",
                display=series.label,
                auto_maintained=True,
            ).model_dump(mode="json"),
        )
    return config


def patient_number(patient, series):
    identifier = PatientIdentifier.objects.filter(
        patient=patient, config=patient_number_config(series)
    ).first()
    return identifier.value if identifier else None


def allocate_patient_number(series, facility, day=None):
    """Returns the next number in the series for the facility's unit, with
    its sequence number: POH-LN-0010294, PRE-LN-0088213 or, counted per day
    (today unless day is given), UNK-LN-20260908-001. Locked like MLC numbers."""
    code = unit_code(facility)
    period = (
        (day or timezone.localdate()).strftime("%Y%m%d")
        if series == PatientNumberSeries.unknown
        else ""
    )
    with transaction.atomic():
        PatientNumberSequence.objects.get_or_create(
            series=series, facility=facility, period=period
        )
        sequence = PatientNumberSequence.objects.select_for_update().get(
            series=series, facility=facility, period=period
        )
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    if period:
        return (
            f"{series}-{code}-{period}-{sequence.last_value:03d}",
            sequence.last_value,
        )
    return f"{series}-{code}-{sequence.last_value:07d}", sequence.last_value


def attach_patient_number(patient, series, value):
    PatientIdentifier.objects.create(
        patient=patient, config=patient_number_config(series), value=value
    )
    patient.build_instance_identifiers()
    patient.save(update_fields=["instance_identifiers"])


def issue_patient_number(patient, facility, series=PatientNumberSeries.uhid):
    """Gives the patient a number in the series unless they already have one,
    and returns it."""
    value = patient_number(patient, series)
    if value is None:
        value, _ = allocate_patient_number(series, facility)
        attach_patient_number(patient, series, value)
    return value


def seats_for_day(availability, day):
    """Seats the availability offers on the day, from Care's own slot
    generator so availability exceptions are left out the same way."""
    windows = [
        {
            "availability": window,
            "slot_size_in_minutes": availability.slot_size_in_minutes,
            "availability_id": availability.id,
        }
        for window in availability.availability
        if window["day_of_week"] == day.weekday()
    ]
    exceptions = AvailabilityException.objects.filter(
        resource=availability.schedule.resource,
        valid_from__lte=day,
        valid_to__gte=day,
    )
    slots = convert_availability_and_exceptions_to_slots(windows, exceptions, day)
    return len(slots) * (availability.tokens_per_slot or 0)


def check_walk_in_reserve(token_slot, booking_channel):
    """Refuses a scheduled booking that would eat into the day's walk-in seats.
    Must run inside the booking's transaction: the reserve row stays locked
    until it commits, so concurrent bookings are counted one at a time."""
    day = timezone.localtime(token_slot.start_datetime).date()
    if booking_channel == BookingChannels.walk_in:
        if day != timezone.localdate():
            raise ValidationError("Walk-in bookings can only be made for today")
        return
    reserve = (
        WalkInReserve.objects.select_for_update()
        .filter(availability=token_slot.availability)
        .first()
    )
    if reserve is None:
        return
    seats = seats_for_day(token_slot.availability, day)
    scheduled = (
        TokenBooking.objects.filter(
            token_slot__availability=token_slot.availability,
            token_slot__start_datetime__date=day,
        )
        .exclude(status__in=CANCELLED_STATUS_CHOICES)
        .exclude(desk_details__booking_channel=BookingChannels.walk_in)
        .count()
    )
    if scheduled >= seats - seats // reserve.walk_in_every:
        raise ValidationError(
            "The remaining seats in this session are held for walk-ins"
        )


def default_visit_purpose(token_slot, patient):
    """Follow-up when the patient saw the same resource within the schedule's
    revisit window, otherwise a new consultation."""
    schedule = token_slot.availability.schedule
    if not schedule.revisit_allowed_days:
        return VisitPurposes.new
    since = token_slot.start_datetime - datetime.timedelta(
        days=schedule.revisit_allowed_days
    )
    earlier = (
        TokenBooking.objects.filter(
            patient=patient,
            token_slot__resource=token_slot.resource,
            token_slot__start_datetime__gte=since,
            token_slot__start_datetime__lt=token_slot.start_datetime,
        )
        .exclude(status__in=[*CANCELLED_STATUS_CHOICES, "noshow"])
        .exists()
    )
    return VisitPurposes.follow_up if earlier else VisitPurposes.new
