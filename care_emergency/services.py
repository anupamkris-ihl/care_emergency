import datetime

from care.emr.api.viewsets.scheduling.availability import (
    convert_availability_and_exceptions_to_slots,
)
from care.emr.models import AvailabilityException, FileUpload, TokenBooking
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
    DOCUMENT_FILE_CATEGORY,
    PATIENT_NUMBER_SYSTEMS,
    BookingChannels,
    DocumentCategories,
    DocumentSides,
    PatientNumberSeries,
    VisitPurposes,
)
from care_emergency.models import (
    AppointmentSequence,
    FacilityUnit,
    MlcSequence,
    PatientDocument,
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


def allocate_document_number(series, facility, seed=None):
    """Returns the next billing document number for the facility's unit,
    counted per calendar year: INV/LN/26/00001, RCT/LN/26/00001. seed(prefix)
    gives the highest number already issued with that prefix, used once when
    the year's counter is created. Locked like MLC numbers."""
    code = unit_code(facility)
    year = care_now().year
    prefix = f"{series}/{code}/{year % 100:02d}/"
    with transaction.atomic():
        PatientNumberSequence.objects.get_or_create(
            series=series,
            facility=facility,
            period=str(year),
            defaults={"last_value": seed(prefix) if seed else 0},
        )
        sequence = PatientNumberSequence.objects.select_for_update().get(
            series=series, facility=facility, period=str(year)
        )
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    return f"{prefix}{sequence.last_value:05d}"


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


def store_document(  # noqa: PLR0913
    user,
    upload,
    category,
    side,
    capture_method,
    patient=None,
    source="",
    document_date=None,
):
    """Saves the file as a Care FileUpload of the patient (or staged, with no
    patient yet) and records what it is."""
    with transaction.atomic():
        file = FileUpload(
            name=f"{DocumentCategories(category).label} ({DocumentSides(side).label})",
            internal_name=upload.name,
            associating_id=str(patient.external_id) if patient else "",
            file_type="patient",
            file_category=DOCUMENT_FILE_CATEGORY.get(category, "unspecified"),
            meta={"mime_type": upload.content_type},
            created_by=user,
            updated_by=user,
        )
        file.save()
        file.files_manager.put_object(file, upload.read())
        file.upload_completed = True
        file.save(skip_internal_name=True)
        return PatientDocument.objects.create(
            file=file,
            patient=patient,
            category=category,
            side=side,
            capture_method=capture_method,
            source=source,
            document_date=document_date,
            created_by=user,
            updated_by=user,
        )


def staged_documents(user, ids):
    """The user's own staged documents with these ids, all or nothing."""
    documents = list(
        PatientDocument.objects.select_for_update().filter(
            external_id__in=ids, patient__isnull=True, created_by=user
        )
    )
    if len(documents) != len(set(ids)):
        raise ValidationError("A document is missing or already used; upload it again")
    return documents


def attach_documents(documents, patient):
    for document in documents:
        document.patient = patient
        document.save(update_fields=["patient", "modified_date"])
        document.file.associating_id = str(patient.external_id)
        document.file.save(update_fields=["associating_id", "modified_date"])


def _complete(documents, category, *, both_sides=False):
    sides = {x.side for x in documents if x.category == category}
    if both_sides:
        return (
            DocumentSides.single in sides
            or {
                DocumentSides.front,
                DocumentSides.back,
            }
            <= sides
        )
    return bool(sides)


def missing_documents(registration, documents, couple=None, partner_documents=()):
    """What v12.1 makes mandatory and the documents don't cover: the payer
    card for any payer but self (R25, front and back unless it is a one-page
    letter), passport and visa for foreign nationals (D43), and both IDs and
    the marriage certificate for an IVF couple (R51)."""
    missing = []
    if registration.payer_type not in ("Self", "International") and not _complete(
        documents, DocumentCategories.payer_card, both_sides=True
    ):
        missing.append("the payer card (front and back)")
    if registration.nationality_category == "Foreign":
        if not _complete(documents, DocumentCategories.passport):
            missing.append("the passport")
        if registration.visa_type != "Not required" and not _complete(
            documents, DocumentCategories.visa
        ):
            missing.append("the visa")
    if couple is not None:
        if not _complete(documents, DocumentCategories.government_id):
            missing.append("the woman's government ID")
        if not couple.single_woman:
            if not _complete(documents, DocumentCategories.marriage_certificate):
                missing.append("the marriage certificate")
            if not _complete(partner_documents, DocumentCategories.government_id):
                missing.append("the partner's government ID")
    return missing
