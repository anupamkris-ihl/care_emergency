import datetime
import re
import secrets

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.api.viewsets.file_upload import file_authorizer
from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.api.viewsets.scheduling.availability import (
    SlotViewSet,
    lock_create_appointment,
)
from care.emr.api.viewsets.scheduling.booking import TokenBookingViewSet
from care.emr.locks.billing import PatientCreateLock
from care.emr.models import ActivityDefinition, Encounter, FacilityOrganization, Patient
from care.emr.models.organization import FacilityOrganizationUser
from care.emr.models.patient import PatientIdentifier
from care.emr.models.scheduling.booking import TokenSlot
from care.emr.models.scheduling.schedule import Availability
from care.emr.resources.encounter.constants import COMPLETED_CHOICES
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.emr.resources.scheduling.slot.spec import (
    BookingStatusChoices,
    TokenBookingReadSpec,
)
from care.facility.models import Facility
from care.security.authorization import AuthorizationController
from care.users.models import User
from care.utils.lock import ObjectLocked
from care.utils.shortcuts import get_object_or_404
from care.utils.sms import send_text_message
from care.utils.time_util import care_now
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import status
from django.db import IntegrityError
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from care_emergency.api_keys import key_state, new_key
from care_emergency.app_server import (
    check_service_patient,
    link_app_patient,
    linked_patients,
)
from care_emergency.constants import (
    APP_SERVER_USERNAME,
    BREAK_GLASS_HOURS,
    QUICK_FORM_LINK_DAYS,
    DOCUMENT_MIME_TYPES,
    REMINDER_LEAD_TIME_HOURS,
    TRIAGE_PRIORITY,
    BookingChannels,
    PatientNumberSeries,
)
from care_emergency.models import (
    ApiKey,
    AppPatientLink,
    AppointmentDetails,
    ArtCouple,
    BreakGlassAccess,
    EmergencyDesk,
    EmergencyEncounter,
    FacilityUnit,
    NewbornBirth,
    PatientDocument,
    PatientRegistration,
    PostOffice,
    QuickRegistration,
    RegistrationAllergy,
    RestrictedPatient,
    StatutoryFlag,
    WalkInReserve,
)
from care_emergency.contact import is_desk_phone
from care_emergency.lab_catalogue import (
    LabTestCreateSpec,
    LabTestWriteSpec,
    check_codes,
    facility_slug,
    lab_slug,
    lab_tariff,
    lab_test_row,
    lab_tests,
    save_lab_test,
)
from care_emergency.merge import (
    can_merge,
    is_unconverted_emergency,
    last_visits,
    merge_preview,
    merge_patients,
    resolve_merged,
)
from care_emergency.newborn import baby_name, baby_registration, serialize_birth
from care_emergency.pincodes import lookup as lookup_pincode
from care_emergency.pincodes import plug_setting
from care_emergency.restricted import can_see_identity, new_alias, restriction_of
from care_emergency.serializers import (
    serialize_appointment,
    serialize_desk,
    serialize_document,
    serialize_emergency,
    serialize_quick_registration,
    serialize_registration,
    serialize_restricted_registration,
    serialize_restriction,
    serialize_unit,
    serialize_walk_in_reserve,
)
from care_emergency.services import (
    allocate_appointment_number,
    allocate_mlc_number,
    allocate_patient_number,
    attach_documents,
    attach_patient_number,
    check_walk_in_reserve,
    default_visit_purpose,
    display_numbers,
    issue_patient_number,
    missing_documents,
    patient_number,
    staged_documents,
    store_document,
    unit_code,
)
from care_emergency.specs import (
    ApiKeyCreateSpec,
    AppRegisterSpec,
    AppointmentBookSpec,
    AppointmentCancelSpec,
    AppointmentRescheduleSpec,
    AppointmentUpdateSpec,
    BreakGlassSpec,
    DeskRegisterSpec,
    DeskRegistrationUpdateSpec,
    DeskContactSpec,
    RegistrationSpec,
    DeskWriteSpec,
    DocumentUploadSpec,
    EmergencyRegisterSpec,
    EmergencyUpdateSpec,
    MedicoLegalMixin,
    NewbornRegisterSpec,
    PatientMergeSpec,
    PatientNumberIssueSpec,
    PatientSummarySpec,
    QuickFormSpec,
    QuickRegisterSpec,
    StatutoryFlagSpec,
    UnitWriteSpec,
    WalkInReserveWriteSpec,
)


def care_view(viewset_class, request, action, **kwargs):
    """A Care viewset bound to this request, so its create/update logic
    (validation, authorization, locks, identifiers) runs unchanged."""
    return viewset_class(
        request=request, kwargs=kwargs, action=action, format_kwarg=None
    )


def get_facility(external_id):
    if not external_id:
        raise ValidationError("facility is required")
    return get_object_or_404(Facility, external_id=external_id)


def can(permission, user, obj):
    return AuthorizationController.call(permission, user, obj)


def can_on_resource(permission, user, resource):
    """Care's schedule and booking checks take the resource first."""
    return AuthorizationController.call(permission, resource, user)


class EmergencyAPIView(APIView):
    def get_exception_handler(self):
        return emr_exception_handler


class DeskView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not (
            can("can_create_encounter_obj", request.user, facility)
            or can("can_update_facility_obj", request.user, facility)
        ):
            raise PermissionDenied("You do not have access to this facility")
        desk = get_object_or_404(EmergencyDesk, facility=facility)
        return Response(serialize_desk(desk))

    def put(self, request):
        spec = DeskWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_update_facility_obj", request.user, facility):
            raise PermissionDenied("Only facility admins can set the desk number")
        desk, _ = EmergencyDesk.objects.update_or_create(
            facility=facility,
            defaults={"phone_number": spec.phone_number, "updated_by": request.user},
            create_defaults={
                "phone_number": spec.phone_number,
                "created_by": request.user,
                "updated_by": request.user,
            },
        )
        return Response(serialize_desk(desk))


class UnitView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not (
            can("can_create_encounter_obj", request.user, facility)
            or can("can_update_facility_obj", request.user, facility)
        ):
            raise PermissionDenied("You do not have access to this facility")
        unit = get_object_or_404(FacilityUnit, facility=facility)
        return Response(serialize_unit(unit))

    def put(self, request):
        spec = UnitWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_update_facility_obj", request.user, facility):
            raise PermissionDenied("Only facility admins can set the unit code")
        unit = FacilityUnit.objects.filter(facility=facility).first()
        if unit and unit.code != spec.code:
            # Issued numbers carry the code, so changing it would split the unit's series.
            raise ValidationError("The unit code can't be changed once set")
        if (
            FacilityUnit.objects.filter(code=spec.code)
            .exclude(facility=facility)
            .exists()
        ):
            raise ValidationError("Another facility already uses this unit code")
        if unit is None:
            unit = FacilityUnit.objects.create(
                facility=facility,
                code=spec.code,
                created_by=request.user,
                updated_by=request.user,
            )
        return Response(serialize_unit(unit))


class PatientNumberView(EmergencyAPIView):
    """Issues the patient's UHID if they don't have one yet: on payment for a
    pre-registration, or for a patient registered through Care's own form."""

    def post(self, request, patient_id):
        spec = PatientNumberIssueSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to update this patient")
        facility = get_facility(spec.facility)
        if issue_number_blocked(patient):
            raise ValidationError(
                "Complete this emergency patient's registration to issue a UHID"
            )
        with transaction.atomic():
            issue_patient_number(patient, facility)
        return Response(PatientRetrieveSpec.serialize(patient).to_json())


def issue_number_blocked(patient):
    return EmergencyEncounter.objects.filter(
        patient=patient, converted_at__isnull=True, is_unidentified=True
    ).exists()


class RegisterView(EmergencyAPIView):
    """Creates the patient, the emergency encounter and the emergency record
    in one transaction, so a failure never leaves an orphan patient."""

    def post(self, request):
        spec = EmergencyRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_create_encounter_obj", request.user, facility):
            raise PermissionDenied("You do not have permission to create encounter")
        desk = EmergencyDesk.objects.filter(facility=facility).first()
        if desk is None:
            raise ValidationError(
                "Set the emergency desk number for this facility first"
            )

        unit_code(facility)

        with transaction.atomic():
            patient_body = {**spec.patient, "phone_number": desk.phone_number}
            unknown_number = None
            if spec.is_unidentified:
                # Staff see unidentified patients as "Unknown <n>", n being the
                # day's sequence in UNK-<unit>-<yyyymmdd>-<nnn>.
                unknown_number, sequence = allocate_patient_number(
                    PatientNumberSeries.unknown, facility
                )
                patient_body["name"] = f"Unknown {sequence}"
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                patient_body
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            if unknown_number:
                # Before the encounter, so arrival doesn't issue a UHID.
                attach_patient_number(
                    patient, PatientNumberSeries.unknown, unknown_number
                )
            encounter_data = care_view(
                EncounterViewSet, request, "create"
            ).handle_create(
                {
                    "patient": str(patient.external_id),
                    "facility": str(facility.external_id),
                    "organizations": [str(x) for x in spec.organizations],
                    "status": "in_progress",
                    "encounter_class": "emer",
                    "priority": TRIAGE_PRIORITY[spec.triage],
                    "hospitalization": {"admit_source": "emd"},
                    "period": {"start": care_now().isoformat()},
                }
            )
            encounter = Encounter.objects.get(external_id=encounter_data["id"])
            brought_by = spec.brought_by
            record = EmergencyEncounter(
                encounter=encounter,
                patient=patient,
                facility=facility,
                triage=spec.triage,
                is_medico_legal=spec.is_medico_legal,
                mlc_nature=spec.mlc_nature,
                police_station=spec.police_station.strip(),
                distinguishing_marks=spec.distinguishing_marks.strip(),
                chief_complaint=spec.chief_complaint,
                mode_of_arrival=spec.mode_of_arrival,
                is_unidentified=spec.is_unidentified,
                brought_by_name=brought_by.name.strip() if brought_by else "",
                brought_by_phone=(brought_by.phone_number or "") if brought_by else "",
                brought_by_relationship=brought_by.relationship if brought_by else "",
                created_by=request.user,
                updated_by=request.user,
            )
            if spec.is_medico_legal:
                record.mlc_number = allocate_mlc_number(facility)
            record.save()
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()

        return Response(
            {
                "patient": patient_data,
                "encounter": encounter_data,
                "emergency": serialize_emergency(record),
            }
        )


class EmergencyEncounterFilters(filters.FilterSet):
    triage = filters.CharFilter(field_name="triage")
    is_medico_legal = filters.BooleanFilter(field_name="is_medico_legal")
    is_unidentified = filters.BooleanFilter(field_name="is_unidentified")
    mlc_number = filters.CharFilter(field_name="mlc_number", lookup_expr="iexact")
    chief_complaint = filters.CharFilter(field_name="chief_complaint")
    mode_of_arrival = filters.CharFilter(field_name="mode_of_arrival")
    patient = filters.UUIDFilter(field_name="patient__external_id")


class EmergencyEncounterViewSet(MedicoLegalMixin, GenericViewSet):
    lookup_field = "encounter__external_id"
    lookup_url_kwarg = "external_id"
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = EmergencyEncounterFilters

    def get_exception_handler(self):
        return emr_exception_handler

    def get_queryset(self):
        qs = EmergencyEncounter.objects.select_related(
            "encounter", "patient", "facility", "created_by"
        ).order_by("-created_date")
        if self.action == "list":
            facility = get_facility(self.request.query_params.get("facility"))
            encounters = AuthorizationController.call(
                "get_filtered_encounters",
                Encounter.objects.all(),
                self.request.user,
                facility,
            )
            return qs.filter(facility=facility, encounter__in=encounters)
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([serialize_emergency(x) for x in page])

    def retrieve(self, request, *args, **kwargs):
        record = self.get_object()
        if not (
            can("can_view_encounter_obj", request.user, record.encounter)
            or can("can_view_patient_obj", request.user, record.patient)
        ):
            raise PermissionDenied("You do not have permission to view this encounter")
        return Response(serialize_emergency(record))

    def partial_update(self, request, *args, **kwargs):
        record = self.get_object()
        if not can("can_update_encounter_obj", request.user, record.encounter):
            raise PermissionDenied("You do not have permission to update encounter")
        spec = EmergencyUpdateSpec.model_validate(request.data)
        for field in spec.model_fields_set - {"brought_by"}:
            value = getattr(spec, field)
            if value is None and field != "is_medico_legal":
                continue
            setattr(record, field, value.strip() if isinstance(value, str) else value)
        if "brought_by" in spec.model_fields_set:
            record.brought_by_name = (
                spec.brought_by.name.strip() if spec.brought_by else ""
            )
            record.brought_by_phone = (
                (spec.brought_by.phone_number or "") if spec.brought_by else ""
            )
            record.brought_by_relationship = (
                spec.brought_by.relationship if spec.brought_by else ""
            )
        try:
            self.check_medico_legal(
                record.is_medico_legal, record.mlc_nature, record.police_station
            )
        except ValueError as e:
            raise ValidationError(str(e)) from e
        if not record.is_medico_legal:
            record.mlc_nature = ""
            record.police_station = ""
        elif not record.mlc_number:
            record.mlc_number = allocate_mlc_number(record.facility)
        # An issued MLC number is kept even if the case is later marked
        # non-medico-legal, so numbers are never reused.
        record.updated_by = request.user
        record.save()
        return Response(serialize_emergency(record))


class ConvertView(EmergencyAPIView):
    """Turns an emergency registration into a normal patient: updates the
    patient through Care's own update path and replaces the desk number."""

    def post(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        records = EmergencyEncounter.objects.filter(
            patient=patient, converted_at__isnull=True
        )
        if not records.exists():
            raise ValidationError(
                "This patient has no emergency registration to convert"
            )
        if not request.data.get("phone_number"):
            raise ValidationError("Enter the patient's own phone number")

        with transaction.atomic():
            view = care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            )
            patient_data = view.handle_update(patient, request.data)
            patient.refresh_from_db(fields=["phone_number"])
            if EmergencyDesk.objects.filter(phone_number=patient.phone_number).exists():
                raise ValidationError(
                    "Use the patient's own number, not the emergency desk number"
                )
            facility = records.order_by("-created_date").first().facility
            unit_code(facility)
            records.update(
                converted_at=care_now(),
                converted_by=request.user,
                is_unidentified=False,
                updated_by=request.user,
            )
            # The UNK number stays on the record alongside the new UHID.
            issue_patient_number(patient, facility)
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()
        return Response(
            {
                "patient": patient_data,
                "emergency": [
                    serialize_emergency(x)
                    for x in EmergencyEncounter.objects.filter(
                        patient=patient
                    ).select_related("encounter", "patient", "facility", "created_by")
                ],
            }
        )


def registration_columns(registration):
    """PatientRegistration column values from a RegistrationSpec. Phone
    numbers are optional; the columns keep "" for none."""
    details = registration.model_dump(exclude={"allergies"})
    return {
        key: "" if value is None and key.endswith(("_number", "_phone")) else value
        for key, value in details.items()
    }


class RegistrationView(EmergencyAPIView):
    """Front-desk registration: creates the Care patient, its
    PatientRegistration row, an IVF partner and the desk documents in one
    transaction."""

    def post(self, request):
        spec = DeskRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = registration_columns(spec.registration)
        patient_body = dict(spec.patient)
        if spec.registration.mobile_owner == "No phone":
            if not can("can_register_patient_at_facility_obj", request.user, facility):
                raise PermissionDenied("You do not have access to this facility")
            desk = EmergencyDesk.objects.filter(facility=facility).first()
            if desk is None:
                raise ValidationError(
                    "Set the emergency desk number for this facility first"
                )
            patient_body["phone_number"] = desk.phone_number
        elif is_desk_phone(patient_body.get("phone_number")):
            raise ValidationError("Select No phone instead of entering the desk number")
        couple = spec.couple
        partner = couple.partner if couple else None
        if set(spec.documents) & set(partner.documents if partner else []):
            raise ValidationError("A document can't belong to both partners")

        if facility:
            unit_code(facility)

        with transaction.atomic():
            documents = staged_documents(request.user, spec.documents)
            partner_documents = (
                staged_documents(request.user, partner.documents) if partner else []
            )
            missing = missing_documents(
                spec.registration, documents, couple, partner_documents
            )
            if missing:
                err = f"Upload {', '.join(missing)}"
                raise ValidationError(err)
            series = (
                PatientNumberSeries.pre_registration
                if spec.pre_registration
                else PatientNumberSeries.uhid
            )
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                patient_body
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            if facility:
                issue_patient_number(patient, facility, series)
                patient_data = PatientRetrieveSpec.serialize(patient).to_json()
            record = PatientRegistration.objects.create(
                patient=patient,
                facility=facility,
                vip_flagged_by=request.user if spec.registration.is_vip else None,
                created_by=request.user,
                updated_by=request.user,
                **details,
            )
            RegistrationAllergy.objects.bulk_create(
                RegistrationAllergy(registration=record, **allergy.model_dump())
                for allergy in spec.registration.allergies
            )
            attach_documents(documents, patient)
            if couple:
                self.register_couple(
                    request, spec, patient, facility, series, partner_documents
                )
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()

        return Response(
            {"patient": patient_data, "registration": serialize_registration(record)}
        )

    def register_couple(self, request, spec, woman, facility, series, documents):
        """The partner becomes his own Care patient, at the woman's address."""
        couple = spec.couple
        partner_patient = None
        if couple.partner:
            partner = couple.partner
            # Care holds one patient-create lock until commit, so a second
            # create in the same transaction would be refused. Its job is
            # identifier uniqueness checks, and the partner has no
            # staff-entered identifiers.
            PatientCreateLock().release()
            data = care_view(PatientViewSet, request, "create").handle_create(
                {
                    "name": partner.name,
                    "gender": "male",
                    "date_of_birth": partner.date_of_birth.isoformat(),
                    "phone_number": partner.phone_number,
                    "address": spec.patient.get("address"),
                    "permanent_address": spec.patient.get("permanent_address"),
                    "pincode": spec.patient.get("pincode"),
                    "geo_organization": spec.patient.get("geo_organization"),
                }
            )
            partner_patient = Patient.objects.get(external_id=data["id"])
            if facility:
                issue_patient_number(partner_patient, facility, series)
            attach_documents(documents, partner_patient)
        ArtCouple.objects.create(
            woman=woman,
            partner=partner_patient,
            facility=facility,
            single_woman=couple.single_woman,
            art_consent=couple.art_consent,
            partner_identity_card_type=couple.partner.identity_card_type
            if couple.partner
            else "",
            partner_identity_number_last4=couple.partner.identity_number_last4
            if couple.partner
            else "",
            partner_abha_number=couple.partner.abha_number if couple.partner else "",
            partner_abha_address=couple.partner.abha_address if couple.partner else "",
            created_by=request.user,
            updated_by=request.user,
        )


class NewbornRegistrationView(EmergencyAPIView):
    """Registers the babies of one delivery: each baby becomes their own Care
    patient with their own UHID and a desk registration inherited from the
    mother's, linked to her by a NewbornBirth row. One transaction, so twins
    are registered together or not at all."""

    def post(self, request):
        spec = NewbornRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        mother = get_object_or_404(Patient, external_id=spec.mother)
        if not can("can_view_patient_obj", request.user, mother):
            raise PermissionDenied("You do not have permission to view the mother")
        if restriction_of(mother) and not can_see_identity(request.user, mother):
            # The baby's name would carry the mother's masked identity.
            raise PermissionDenied(
                "The mother's record is restricted. Ask her clinician to register the baby"
            )
        if mother.gender != "female":
            raise ValidationError("The mother's record must be a female patient")
        if is_unconverted_emergency(mother):
            raise ValidationError(
                "Complete the mother's emergency registration before registering the baby"
            )
        mother_record = PatientRegistration.objects.filter(patient=mother).first()
        geo_organization = mother.geo_organization or facility.geo_organization
        unit_code(facility)

        number_of_babies = len(spec.babies)
        babies = []
        with transaction.atomic():
            for order, baby in enumerate(spec.babies, start=1):
                # Care holds one patient-create lock until commit; the babies
                # have no staff-entered identifiers for it to check.
                PatientCreateLock().release()
                data = care_view(PatientViewSet, request, "create").handle_create(
                    {
                        "name": baby_name(mother.name, order, number_of_babies),
                        "gender": baby.gender,
                        "date_of_birth": timezone.localtime(baby.born_at)
                        .date()
                        .isoformat(),
                        "phone_number": mother.phone_number,
                        "address": mother.address,
                        "permanent_address": mother.permanent_address,
                        "pincode": mother.pincode,
                        "geo_organization": str(geo_organization.external_id)
                        if geo_organization
                        else None,
                    }
                )
                patient = Patient.objects.get(external_id=data["id"])
                issue_patient_number(patient, facility)
                PatientRegistration.objects.create(
                    patient=patient,
                    facility=facility,
                    created_by=request.user,
                    updated_by=request.user,
                    **baby_registration(mother, mother_record, baby.gender),
                )
                birth = NewbornBirth.objects.create(
                    baby=patient,
                    mother=mother,
                    facility=facility,
                    born_at=baby.born_at,
                    birth_order=order,
                    number_of_babies=number_of_babies,
                    delivery_type=spec.delivery_type,
                    gestation_weeks=spec.gestation_weeks,
                    birth_weight_grams=baby.birth_weight_grams,
                    length_cm=baby.length_cm,
                    head_circumference_cm=baby.head_circumference_cm,
                    apgar_1_min=baby.apgar_1_min,
                    apgar_5_min=baby.apgar_5_min,
                    created_by=request.user,
                    updated_by=request.user,
                )
                patient.refresh_from_db()
                babies.append(
                    {
                        "patient": PatientRetrieveSpec.serialize(patient).to_json(),
                        "birth": serialize_birth(birth),
                    }
                )

        return Response(
            {
                "mother": str(mother.external_id),
                "inherited": mother_record is not None,
                "babies": babies,
            }
        )


class PatientRegistrationView(EmergencyAPIView):
    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        record = get_object_or_404(
            PatientRegistration.objects.select_related(
                "patient", "facility", "vip_flagged_by", "created_by"
            ),
            patient=patient,
        )
        restriction = restriction_of(patient)
        if restriction and not can_see_identity(request.user, patient):
            return Response(serialize_restricted_registration(record, restriction))
        return Response(serialize_registration(record))

    def put(self, request, patient_id):
        """Edits a registered patient with the registration form: Care's own
        patient update, then the desk record (created for a patient who has
        none, e.g. one registered before the plug). The UHID is kept."""
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to edit this patient")
        if restriction_of(patient) and not can_see_identity(request.user, patient):
            raise PermissionDenied("This patient's record is restricted")
        if EmergencyEncounter.objects.filter(
            patient=patient, converted_at__isnull=True
        ).exists():
            raise ValidationError("Complete the patient's emergency registration first")
        spec = DeskRegistrationUpdateSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = registration_columns(spec.registration)
        patient_body = dict(spec.patient)
        if spec.registration.mobile_owner == "No phone":
            desk = EmergencyDesk.objects.filter(facility=facility).first()
            if desk is None:
                raise ValidationError(
                    "Set the emergency desk number for this facility first"
                )
            patient_body["phone_number"] = desk.phone_number
        elif is_desk_phone(patient_body.get("phone_number")):
            raise ValidationError("Select No phone instead of entering the desk number")

        with transaction.atomic():
            documents = staged_documents(request.user, spec.documents)
            existing = list(PatientDocument.objects.filter(patient=patient))
            missing = missing_documents(spec.registration, existing + documents)
            if missing:
                err = f"Upload {', '.join(missing)}"
                raise ValidationError(err)
            care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            ).handle_update(patient, patient_body)
            record = PatientRegistration.objects.filter(patient=patient).first()
            if record is None:
                record = PatientRegistration(
                    patient=patient, facility=facility, created_by=request.user
                )
            for key, value in details.items():
                setattr(record, key, value)
            # Whoever first flagged the VIP stays recorded while the flag is on.
            if not spec.registration.is_vip:
                record.vip_flagged_by = None
            elif record.vip_flagged_by is None:
                record.vip_flagged_by = request.user
            record.updated_by = request.user
            record.save()
            record.allergies.all().delete()
            RegistrationAllergy.objects.bulk_create(
                RegistrationAllergy(registration=record, **allergy.model_dump())
                for allergy in spec.registration.allergies
            )
            attach_documents(documents, patient)
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()

        return Response(
            {"patient": patient_data, "registration": serialize_registration(record)}
        )


def quick_form_link(record):
    base = plug_setting("REGISTRATION_FORM_BASE_URL") or settings.CURRENT_DOMAIN
    return f"{base.rstrip('/')}/register/complete/{record.form_token}"


def send_quick_form_link(record_id):
    """Texts the patient the full registration form. Runs after commit, so a
    rolled-back registration never sends a link."""
    record = QuickRegistration.objects.select_related("patient", "facility").get(
        id=record_id
    )
    sent = send_text_message(
        content=(
            f"{record.facility.name}: Hi {record.first_name}, please complete "
            f"your registration before your visit: {quick_form_link(record)} "
            f"(valid for {QUICK_FORM_LINK_DAYS} days)"
        ),
        recipients=record.patient.phone_number,
        fail_silently=True,
    )
    if sent:
        QuickRegistration.objects.filter(id=record.id).update(
            form_link_sent_at=timezone.now()
        )
    return bool(sent)


class QuickRegistrationView(EmergencyAPIView):
    """Quick Register (phone booking): creates the Care patient with a PRE-
    reference and the QuickRegistration row that marks how they were
    registered, then texts the full registration form if asked."""

    @classmethod
    def as_view(cls, **initkwargs):
        # Commits before responding, so the response says whether the link
        # was actually texted (it is sent once the patient is saved).
        return transaction.non_atomic_requests(super().as_view(**initkwargs))

    def post(self, request):
        spec = QuickRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter the patient's own mobile number")
        department = None
        if spec.department:
            department = get_object_or_404(
                FacilityOrganization,
                external_id=spec.department,
                facility=facility,
                org_type="dept",
            )
        doctor = None
        if spec.doctor:
            doctor = get_object_or_404(User, external_id=spec.doctor)
            if not FacilityOrganizationUser.objects.filter(
                user=doctor, organization__facility=facility
            ).exists():
                raise ValidationError("The doctor does not work at this facility")
        unit_code(facility)

        with transaction.atomic():
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                {
                    "name": f"{spec.first_name} {spec.last_name}".strip(),
                    "gender": spec.gender,
                    "phone_number": spec.phone_number,
                    "date_of_birth": spec.date_of_birth.isoformat(),
                }
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            issue_patient_number(
                patient, facility, PatientNumberSeries.pre_registration
            )
            record = QuickRegistration.objects.create(
                patient=patient,
                facility=facility,
                title=spec.title,
                first_name=spec.first_name,
                last_name=spec.last_name,
                consultation_type=spec.consultation_type,
                department=department,
                doctor=doctor,
                whatsapp_same_as_mobile=spec.whatsapp_same_as_mobile,
                send_form_link=spec.send_form_link,
                send_payment_reminder=spec.send_payment_reminder,
                form_token=secrets.token_urlsafe(32),
                form_expires_at=timezone.now()
                + datetime.timedelta(days=QUICK_FORM_LINK_DAYS),
                created_by=request.user,
                updated_by=request.user,
            )
            if spec.send_form_link:
                transaction.on_commit(lambda: send_quick_form_link(record.id))
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()
        record.refresh_from_db()

        return Response(
            {
                "patient": patient_data,
                "quick_registration": serialize_quick_registration(record),
            }
        )


class PatientQuickRegistrationView(EmergencyAPIView):
    def get_record(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        return get_object_or_404(
            QuickRegistration.objects.select_related(
                "patient", "facility", "department", "doctor", "created_by"
            ),
            patient=patient,
        )

    def get(self, request, patient_id):
        return Response(
            serialize_quick_registration(self.get_record(request, patient_id))
        )


class QuickFormLinkView(PatientQuickRegistrationView):
    """Texts the registration form again (a failed SMS, a lost message) and
    keeps the link open for another QUICK_FORM_LINK_DAYS."""

    http_method_names = ["post"]

    @classmethod
    def as_view(cls, **initkwargs):
        return transaction.non_atomic_requests(super().as_view(**initkwargs))

    def post(self, request, patient_id):
        record = self.get_record(request, patient_id)
        if record.form_submitted_at:
            raise ValidationError("The patient has already submitted the form")
        if is_desk_phone(record.patient.phone_number):
            raise ValidationError("The patient has no mobile number to text")
        record.send_form_link = True
        record.form_expires_at = timezone.now() + datetime.timedelta(
            days=QUICK_FORM_LINK_DAYS
        )
        record.updated_by = request.user
        record.save(
            update_fields=[
                "send_form_link",
                "form_expires_at",
                "updated_by",
                "modified_date",
            ]
        )
        if not send_quick_form_link(record.id):
            raise ValidationError("The message couldn't be sent. Try again later")
        record.refresh_from_db()
        return Response(serialize_quick_registration(record))


class QuickFormThrottle(AnonRateThrottle):
    rate = "30/min"


class QuickFormView(EmergencyAPIView):
    """The texted registration form. Open to the patient without a login: the
    unguessable token is the credential, it expires, and it takes one answer."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = [QuickFormThrottle]

    def get_record(self, token, *, lock=False):
        records = QuickRegistration.objects.select_related("patient", "facility")
        if lock:
            records = records.select_for_update()
        return get_object_or_404(records, form_token=token)

    def get(self, request, token):
        record = self.get_record(token)
        if record.form_submitted_at:
            state = "submitted"
        elif record.form_expires_at <= timezone.now():
            state = "expired"
        else:
            state = "open"
        phone = record.patient.phone_number or ""
        return Response(
            {
                "status": state,
                "first_name": record.first_name,
                "facility_name": record.facility.name,
                "phone_last4": phone[-4:],
                "whatsapp_same_as_mobile": record.whatsapp_same_as_mobile,
                "expires_at": record.form_expires_at,
            }
        )

    def post(self, request, token):
        spec = QuickFormSpec.model_validate(request.data)
        with transaction.atomic():
            record = self.get_record(token, lock=True)
            if record.form_submitted_at:
                raise ValidationError("This form has already been submitted")
            if record.form_expires_at <= timezone.now():
                raise ValidationError(
                    "This link has expired. Please call the hospital for a new one"
                )
            phone = record.patient.phone_number
            if spec.alternate_phone_number and spec.alternate_phone_number == phone:
                raise ValidationError(
                    "The alternate mobile must differ from the main number"
                )
            details = spec.model_dump(mode="json")
            if record.whatsapp_same_as_mobile:
                details["whatsapp_number"] = phone
            record.form_details = details
            record.form_submitted_at = timezone.now()
            record.save(update_fields=["form_details", "form_submitted_at"])
        return Response({"status": "submitted"})


class QuickFormPincodeView(QuickFormView):
    """The PIN lookup for the texted form, open only to a live link."""

    http_method_names = ["get"]

    def get(self, request, token, pincode):
        record = self.get_record(token)
        if record.form_submitted_at or record.form_expires_at <= timezone.now():
            raise PermissionDenied("This link is no longer open")
        return pincode_response(pincode)


class PatientNumberSearchView(EmergencyAPIView):
    """Exact UHID lookup; return only patients the caller can view."""

    def get(self, request):
        value = request.query_params.get("uhid", "").strip().upper()
        if not value or len(value) > 40:
            raise ValidationError("Enter the full UHID")
        # A merged-away record's UHID is kept (soft-deleted) on it, and leads
        # to the record it was merged into.
        ids = PatientIdentifier._base_manager.filter(  # noqa: SLF001
            config__config__system="care_emergency/uhid", value=value
        ).values_list("patient_id", flat=True)
        results = [
            PatientRetrieveSpec.serialize(patient).to_json()
            for patient in Patient.objects.filter(id__in=resolve_merged(ids))
            if can("can_view_patient_obj", request.user, patient)
        ]
        return Response({"count": len(results), "results": results})


class PatientSummaryView(EmergencyAPIView):
    """The patient number and last visit of each search result, in one call.
    Unknown, deleted and not viewable patients are left out."""

    def post(self, request):
        spec = PatientSummarySpec.model_validate(request.data)
        patients = Patient.objects.filter(external_id__in=spec.patients)
        visible = set(
            AuthorizationController.call(
                "get_filtered_patients", patients, request.user
            ).values_list("id", flat=True)
        )
        # Desk access (registration facility) isn't in Care's filter.
        allowed = {
            patient.external_id: patient
            for patient in patients
            if patient.id in visible
            or can("can_view_patient_obj", request.user, patient)
        }
        ids = [x.id for x in allowed.values()]
        numbers = display_numbers(ids)
        visits = last_visits(ids)
        results = [
            {
                "id": str(external_id),
                "patient_number": numbers.get(allowed[external_id].id),
                "last_visit": visits.get(allowed[external_id].id),
            }
            for external_id in dict.fromkeys(spec.patients)
            if external_id in allowed
        ]
        return Response({"results": results})


class PatientMergeView(EmergencyAPIView):
    """Merge a duplicate (source) into the record that is kept (target).
    GET previews it; POST merges. Both need merge authority (can_merge)."""

    def get_pair(self, source_id, target_id, lock=False):
        for value in (source_id, target_id):
            if not value:
                raise ValidationError("source and target are required")
        patients = Patient.objects.all()
        if lock:
            # A stable order, so two merges of the same pair can't deadlock.
            patients = patients.select_for_update().order_by("id")
            list(patients.filter(external_id__in=[source_id, target_id]))
        source = get_object_or_404(patients, external_id=source_id)
        target = get_object_or_404(patients, external_id=target_id)
        if not can_merge(self.request.user, source, target):
            raise PermissionDenied("You do not have permission to merge these records")
        return source, target

    def get(self, request):
        source, target = self.get_pair(
            request.query_params.get("source"), request.query_params.get("target")
        )
        return Response(merge_preview(source, target))

    def post(self, request):
        spec = PatientMergeSpec.model_validate(request.data)
        lock = PatientCreateLock()
        try:
            lock.acquire()
        except ObjectLocked as e:
            raise ValidationError(
                "Another registration is being saved, try again in a moment"
            ) from e
        try:
            with transaction.atomic():
                source, target = self.get_pair(spec.source, spec.target, lock=True)
                merge = merge_patients(
                    source, target, spec.choices, spec.reason, request.user
                )
                transaction.on_commit(lock.release)
        except BaseException:
            lock.release()
            raise
        target.refresh_from_db()
        return Response(
            {
                "patient": PatientRetrieveSpec.serialize(target).to_json(),
                "moved": merge.moved,
            }
        )


class PatientContactView(EmergencyAPIView):
    """Replace a No phone fallback with a real contact, preserving identifiers."""

    def patch(self, request, patient_id):
        spec = DeskContactSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("Cannot update this patient")
        restriction = restriction_of(patient)
        if restriction and not can_see_identity(request.user, patient):
            raise PermissionDenied("Identity access is required to update a contact")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter a real contact number, not the desk number")
        with transaction.atomic():
            record = get_object_or_404(
                PatientRegistration.objects.select_for_update(), patient=patient
            )
            if record.mobile_owner != "No phone":
                raise ValidationError("This patient already has a contact number")
            registration = RegistrationSpec.model_construct(
                **{
                    **serialize_registration(record),
                    "mobile_owner": spec.mobile_owner,
                    "whatsapp_number": record.whatsapp_number or None,
                    "alternate_phone_number": record.alternate_phone_number or None,
                    "next_of_kin_phone": record.next_of_kin_phone or None,
                }
            )
            # Apply contact validation without revalidating unrelated demographics.
            validated = DeskRegisterSpec.model_construct(
                facility=record.facility.external_id,
                patient={"pincode": patient.pincode, "phone_number": spec.phone_number},
                registration=registration,
            )
            try:
                validated.validate_contact()
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if patient.age < 18 and spec.mobile_owner == "Self":
                raise ValidationError(
                    "A minor's mobile must belong to a parent, guardian or relative"
                )
            view = care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            )
            # Care's update spec clears PIN when omitted, even for a partial update.
            view.handle_update(
                patient, {"phone_number": spec.phone_number, "pincode": patient.pincode}
            )
            record.mobile_owner = spec.mobile_owner
            record.updated_by = request.user
            record.save(update_fields=["mobile_owner", "updated_by", "modified_date"])
            patient.refresh_from_db()
        return Response(PatientRetrieveSpec.serialize(patient).to_json())


class DocumentView(EmergencyAPIView):
    """Desk documents (D37, D43). POST is multipart: file, category, side,
    capture_method and, for an existing patient, patient. Without a patient
    the document is staged until a registration uses it."""

    parser_classes = [MultiPartParser]

    def get(self, request):
        patient = get_object_or_404(
            Patient, external_id=request.query_params.get("patient")
        )
        file_authorizer(request.user, "patient", str(patient.external_id), "read")
        if restriction_of(patient) and not can_see_identity(request.user, patient):
            raise PermissionDenied("This patient's documents are restricted")
        documents = PatientDocument.objects.filter(patient=patient).select_related(
            "file", "created_by"
        )
        return Response(
            [serialize_document(x) for x in documents.order_by("-created_date")]
        )

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError("Choose a file")
        # Blank optional multipart fields are left out so their defaults apply.
        spec = DocumentUploadSpec.model_validate(
            {
                key: request.data[key]
                for key in DocumentUploadSpec.model_fields
                if request.data.get(key) not in (None, "")
            }
        )
        if upload.size > settings.MAX_FILE_UPLOAD_SIZE * 1024 * 1024:
            err = f"Files can be up to {settings.MAX_FILE_UPLOAD_SIZE} MB"
            raise ValidationError(err)
        if upload.content_type not in DOCUMENT_MIME_TYPES:
            raise ValidationError("Upload a JPEG, PNG, WebP or PDF")
        patient = None
        if spec.patient:
            patient = get_object_or_404(Patient, external_id=spec.patient)
            file_authorizer(request.user, "patient", str(spec.patient), "write")
        elif not AuthorizationController.call("can_create_patient", request.user):
            raise PermissionDenied("You do not have permission to register patients")
        document = store_document(
            request.user,
            upload,
            spec.category,
            spec.side,
            spec.capture_method,
            patient,
            spec.source,
            spec.document_date,
        )
        return Response(serialize_document(document))


class StagedDocumentView(EmergencyAPIView):
    def delete(self, request, document_id):
        document = get_object_or_404(
            PatientDocument,
            external_id=document_id,
            patient__isnull=True,
            created_by=request.user,
        )
        file = document.file
        document.delete()
        file.files_manager.delete_object(file, quiet=True)
        file.delete()
        return Response(status=204)


class RestrictionView(EmergencyAPIView):
    """Statutory confidentiality (D30). GET tells anyone who can see the
    patient whether they are restricted; the categories are shown only to
    those who can see the identity."""

    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        restriction = restriction_of(patient)
        visible = restriction is not None and can_see_identity(request.user, patient)
        return Response(serialize_restriction(restriction, request.user, visible))


class StatutoryFlagView(EmergencyAPIView):
    """Raised by the treating clinician in an encounter: needs clinical write
    access to it (doctors and nurses), which the front desk doesn't have."""

    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        encounters = (
            Encounter.objects.filter(patient=patient)
            .exclude(status__in=[*COMPLETED_CHOICES, "discharged"])
            .select_related("facility", "patient")
            .order_by("-created_date")
        )
        return Response(
            [
                {
                    "id": str(encounter.external_id),
                    "status": encounter.status,
                    "encounter_class": encounter.encounter_class,
                    "created_date": encounter.created_date,
                }
                for encounter in encounters
                if can(
                    "can_write_service_request_in_encounter", request.user, encounter
                )
            ][:20]
        )

    def post(self, request, patient_id):
        spec = StatutoryFlagSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        encounter = get_object_or_404(
            Encounter, external_id=spec.encounter, patient=patient
        )
        if not can("can_write_service_request_in_encounter", request.user, encounter):
            raise PermissionDenied(
                "Only the treating clinician can raise a statutory flag"
            )
        if encounter.status in [*COMPLETED_CHOICES, "discharged"]:
            raise ValidationError("Statutory flags require an open encounter")
        with transaction.atomic():
            restriction = (
                RestrictedPatient.objects.select_for_update()
                .filter(patient=patient)
                .first()
            ) or RestrictedPatient.objects.create(patient=patient, alias=new_alias())
            StatutoryFlag.objects.get_or_create(
                patient=patient,
                encounter=encounter,
                category=spec.category,
                defaults={"created_by": request.user, "updated_by": request.user},
            )
        return Response(serialize_restriction(restriction, request.user, visible=True))


class BreakGlassView(EmergencyAPIView):
    """Shows a restricted patient's identity to the user for an hour, with
    the reason kept for audit. Anyone who can view the patient can do it;
    every use is recorded."""

    def post(self, request, patient_id):
        spec = BreakGlassSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        restriction = restriction_of(patient)
        if restriction is None:
            raise ValidationError("This patient is not restricted")
        BreakGlassAccess.objects.create(
            patient=patient,
            user=request.user,
            reason=spec.reason.strip(),
            expires_at=timezone.now() + datetime.timedelta(hours=BREAK_GLASS_HOURS),
        )
        return Response(serialize_restriction(restriction, request.user, visible=True))


class AppointmentFilters(filters.FilterSet):
    patient = filters.UUIDFilter(field_name="booking__patient__external_id")
    appointment_number = filters.CharFilter(
        field_name="appointment_number", lookup_expr="iexact"
    )
    date = filters.DateFilter(field_name="booking__token_slot__start_datetime__date")
    booking_channel = filters.CharFilter(field_name="booking_channel")


class AppointmentViewSet(GenericViewSet):
    """Books through Care's own slot booking and adds the desk details in the
    same transaction. Looked up by the Care booking id. A service account
    (the app server) only sees and changes bookings of patients linked to it."""

    lookup_field = "booking__external_id"
    lookup_url_kwarg = "booking_id"
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = AppointmentFilters

    def get_exception_handler(self):
        return emr_exception_handler

    def get_queryset(self):
        qs = AppointmentDetails.objects.select_related(
            "booking",
            "booking__patient",
            "booking__token_slot__resource",
            "facility",
            "created_by",
        ).order_by("-created_date")
        user = self.request.user
        if user.is_service_account:
            qs = qs.filter(booking__patient__in=linked_patients(user))
            facility = self.request.query_params.get("facility")
            if self.action == "list" and facility:
                qs = qs.filter(facility=get_facility(facility))
            return qs
        if self.action == "list":
            facility = get_facility(self.request.query_params.get("facility"))
            if not can("can_list_booking_on_facility", user, facility):
                raise PermissionDenied("You do not have permission to view bookings")
            return qs.filter(facility=facility)
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([serialize_appointment(x) for x in page])

    def retrieve(self, request, *args, **kwargs):
        details = self.get_object()
        booking = details.booking
        if not (
            can_on_resource(
                "can_list_booking", request.user, booking.token_slot.resource
            )
            or can("can_view_patient_obj", request.user, booking.patient)
        ):
            raise PermissionDenied("You do not have permission to view this booking")
        return Response(serialize_appointment(details))

    def create(self, request, *args, **kwargs):
        spec = AppointmentBookSpec.model_validate(request.data)
        slot = get_object_or_404(
            TokenSlot.objects.select_related(
                "resource__facility", "availability__schedule__resource"
            ),
            external_id=spec.slot,
        )
        if not can_on_resource("can_create_booking", request.user, slot.resource):
            raise PermissionDenied("You do not have permission to create appointments")
        patient = get_object_or_404(Patient, external_id=spec.patient)
        check_service_patient(request.user, patient)

        with transaction.atomic():
            # Locks the slot so allocated is current when Care checks capacity.
            slot = TokenSlot.objects.select_for_update().get(pk=slot.pk)
            check_walk_in_reserve(slot, spec.booking_channel)
            booking = SlotViewSet.create_appointment_handler(
                slot,
                {
                    "patient": str(spec.patient),
                    "note": spec.note,
                    "tags": [str(x) for x in spec.tags],
                },
                request.user,
            )
            details = AppointmentDetails.objects.create(
                booking=booking,
                facility=slot.resource.facility,
                appointment_number=allocate_appointment_number(),
                consultation_type=spec.consultation_type,
                visit_purpose=spec.visit_purpose
                or default_visit_purpose(slot, booking.patient),
                booking_channel=spec.booking_channel,
                reminder_due_at=reminder_due(slot, spec.booking_channel),
                guidance_area=spec.guidance_area,
                guidance_since=spec.guidance_since,
                chief_complaint=spec.chief_complaint,
                created_by=request.user,
                updated_by=request.user,
            )

        return Response(
            {
                "booking": TokenBookingReadSpec.serialize(booking).to_json(),
                "details": serialize_appointment(details),
            }
        )

    def get_open_booking(self):
        """The booking to change, if the caller may write it and it hasn't
        been cancelled, attended or started."""
        details = self.get_object()
        booking = details.booking
        if not can_on_resource(
            "can_write_booking", self.request.user, booking.token_slot.resource
        ):
            raise PermissionDenied("You do not have permission to change this booking")
        if booking.status != BookingStatusChoices.booked.value:
            raise ValidationError("Only a booked appointment can be changed")
        return details

    def partial_update(self, request, *args, **kwargs):
        spec = AppointmentUpdateSpec.model_validate(request.data)
        with transaction.atomic():
            details = self.get_open_booking()
            booking = details.booking
            if spec.note is not None:
                booking.note = spec.note
                booking.updated_by = request.user
                booking.save(update_fields=["note", "updated_by", "modified_date"])
            for field in ("consultation_type", "visit_purpose", "chief_complaint"):
                value = getattr(spec, field)
                if value is not None:
                    setattr(details, field, value.strip() if field == "chief_complaint" else value)
            details.updated_by = request.user
            details.save()
        return Response(serialize_appointment(details))

    @action(detail=True, methods=["POST"])
    def cancel(self, request, *args, **kwargs):
        spec = AppointmentCancelSpec.model_validate(request.data)
        with transaction.atomic():
            details = self.get_open_booking()
            TokenBookingViewSet.cancel_appointment_handler(
                details.booking,
                {"reason": spec.reason, "note": spec.note or None},
                request.user,
            )
            details.reminder_due_at = None
            details.updated_by = request.user
            details.save(update_fields=["reminder_due_at", "updated_by", "modified_date"])
        return Response(serialize_appointment(details))

    @action(detail=True, methods=["POST"])
    def reschedule(self, request, *args, **kwargs):
        """Moves the appointment to another slot at the same facility. Care
        marks the old booking rescheduled and makes a new one; the APT number
        and desk details move to the new booking."""
        spec = AppointmentRescheduleSpec.model_validate(request.data)
        with transaction.atomic():
            details = self.get_open_booking()
            old = details.booking
            if not can("can_reschedule_booking", request.user, details.facility):
                raise PermissionDenied(
                    "You do not have permission to reschedule appointments"
                )
            slot = get_object_or_404(
                TokenSlot.objects.select_for_update(of=("self",)).select_related(
                    "resource__facility", "availability"
                ),
                external_id=spec.slot,
                resource__facility=details.facility,
            )
            if slot.id == old.token_slot_id:
                raise ValidationError("Choose a different slot")
            if not can_on_resource("can_create_booking", request.user, slot.resource):
                raise PermissionDenied(
                    "You do not have permission to book this slot"
                )
            check_walk_in_reserve(slot, details.booking_channel)
            TokenBookingViewSet.cancel_appointment_handler(
                old, {"reason": BookingStatusChoices.rescheduled}, request.user
            )
            booking = lock_create_appointment(
                slot, old.patient, request.user, spec.note or old.note
            )
            details.booking = booking
            details.reminder_due_at = reminder_due(slot, details.booking_channel)
            details.reminder_sent_at = None
            details.updated_by = request.user
            details.save()
        return Response(
            {
                "booking": TokenBookingReadSpec.serialize(booking).to_json(),
                "details": serialize_appointment(details),
            }
        )


def reminder_due(slot, booking_channel):
    """Phone bookings get an SMS before the slot, if that is still ahead."""
    if booking_channel != BookingChannels.phone:
        return None
    due = slot.start_datetime - datetime.timedelta(hours=REMINDER_LEAD_TIME_HOURS)
    return due if due > care_now() else None


class WalkInReserveView(EmergencyAPIView):
    def get_availability(self, external_id, permission):
        availability = get_object_or_404(
            Availability.objects.select_related("schedule__resource__facility"),
            external_id=external_id,
        )
        if not can_on_resource(
            permission, self.request.user, availability.schedule.resource
        ):
            raise PermissionDenied("You do not have access to this schedule")
        return availability

    def get(self, request):
        availability = self.get_availability(
            request.query_params.get("availability"), "can_list_schedule"
        )
        reserve = get_object_or_404(WalkInReserve, availability=availability)
        return Response(serialize_walk_in_reserve(reserve))

    def put(self, request):
        spec = WalkInReserveWriteSpec.model_validate(request.data)
        availability = self.get_availability(spec.availability, "can_write_schedule")
        if spec.walk_in_every is None:
            WalkInReserve.objects.filter(availability=availability).delete()
            return Response(status=204)
        reserve, _ = WalkInReserve.objects.update_or_create(
            availability=availability,
            defaults={"walk_in_every": spec.walk_in_every, "updated_by": request.user},
            create_defaults={
                "walk_in_every": spec.walk_in_every,
                "created_by": request.user,
                "updated_by": request.user,
            },
        )
        return Response(serialize_walk_in_reserve(reserve))


def pincode_response(pincode):
    if not re.fullmatch(r"[1-9]\d{5}", pincode):
        raise ValidationError("Enter a 6-digit PIN code.")
    place = lookup_pincode(pincode)
    if place is None:
        loaded = PostOffice.objects.exists()
        detail = (
            "PIN code not found." if loaded else "The PIN directory hasn't been loaded."
        )
        return Response(
            {"detail": detail, "directory_loaded": loaded},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(place)


class PincodeView(EmergencyAPIView):
    """India Post's district, state and post offices for a PIN (D17, M01).
    404 when the PIN isn't listed or the directory isn't loaded yet; callers
    fall back to a live lookup either way."""

    def get(self, request, pincode):
        return pincode_response(pincode)


def can_read_lab_catalogue(user, facility):
    return (
        can("can_list_facility_activity_definition", user, facility)
        or can("can_create_encounter_obj", user, facility)
        or can("can_update_facility_obj", user, facility)
    )


class LabTestView(EmergencyAPIView):
    """The facility's lab test catalogue (Settings → Lab tests, and the front desk's
    lab booking). ?status=active lists only the tests that can be booked."""

    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not can_read_lab_catalogue(request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        status_filter = request.query_params.get("status")
        return Response({"results": lab_tests(facility, status_filter)})

    def post(self, request):
        spec = LabTestCreateSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        check_codes(facility, spec)
        activity = save_lab_test(facility, spec, request.user)
        return Response(lab_test_row(facility, activity), status=status.HTTP_201_CREATED)


class LabTestDetailView(EmergencyAPIView):
    def put(self, request, code):
        spec = LabTestWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        activity = get_object_or_404(
            ActivityDefinition,
            facility=facility,
            slug=facility_slug(facility, lab_slug(code)),
        )
        check_codes(facility, spec, activity)
        save_lab_test(facility, spec, request.user, activity)
        return Response(lab_test_row(facility, activity))


class LabTariffView(EmergencyAPIView):
    """Tariff rows (H2 diagnostics) a lab test can be priced from."""

    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        return Response({"results": lab_tariff(facility)})


def serialize_api_key(api_key):
    return {
        "id": str(api_key.external_id),
        "name": api_key.name,
        "prefix": api_key.prefix,
        "username": api_key.user.username,
        "state": key_state(api_key),
        "created_by": api_key.created_by.username if api_key.created_by else None,
        "created_date": api_key.created_date,
        "expires_at": api_key.expires_at,
        "revoked_at": api_key.revoked_at,
        "last_used_at": api_key.last_used_at,
    }


def username_error(message):
    return ValidationError({"errors": [{"loc": ["username"], "msg": message}]})


def check_key_admin(request):
    # A key can't manage keys, even a superuser's, so a leaked key can't mint more.
    if isinstance(request.auth, ApiKey) or not request.user.is_superuser:
        raise PermissionDenied("Only administrators can manage API keys")


class ApiKeyView(EmergencyAPIView):
    """Settings → API keys: keys for clients such as the patient mobile app."""

    def get(self, request):
        check_key_admin(request)
        keys = ApiKey.objects.filter(deleted=False).select_related(
            "user", "created_by"
        )
        return Response(
            {"results": [serialize_api_key(key) for key in keys.order_by("-id")]}
        )

    def post(self, request):
        check_key_admin(request)
        spec = ApiKeyCreateSpec.model_validate(request.data)
        user = User.objects.filter(username=spec.username, deleted=False).first()
        if user is None and spec.username == APP_SERVER_USERNAME:
            raise username_error(
                "The app server account isn't set up yet; run setup_app_server"
            )
        if user is None or not user.is_active:
            raise username_error("No active user has this username")
        if user.is_superuser:
            # A leaked key would hand out the whole system.
            raise username_error("A key can't act as a superuser; use a staff user")
        key, prefix, key_hash = new_key()
        api_key = ApiKey.objects.create(
            name=spec.name,
            user=user,
            prefix=prefix,
            key_hash=key_hash,
            expires_at=spec.expires_at,
            created_by=request.user,
            updated_by=request.user,
        )
        # The only time the key is sent: it can't be recovered from its hash.
        return Response(
            {**serialize_api_key(api_key), "key": key},
            status=status.HTTP_201_CREATED,
        )


class ApiKeyRevokeView(EmergencyAPIView):
    def post(self, request, key_id):
        check_key_admin(request)
        api_key = get_object_or_404(ApiKey, external_id=key_id, deleted=False)
        if api_key.revoked_at is None:
            api_key.revoked_at = timezone.now()
            api_key.updated_by = request.user
            api_key.save(update_fields=["revoked_at", "updated_by", "modified_date"])
        return Response(serialize_api_key(api_key))


def abha_matches(abha_number):
    """Live patients with this ABHA number, in Care's registration details or
    the desk record."""
    ids = set(
        Patient.objects.filter(
            meta__registration__abha_number=abha_number
        ).values_list("id", flat=True)
    ) | set(
        PatientRegistration.objects.filter(abha_number=abha_number).values_list(
            "patient_id", flat=True
        )
    )
    return list(Patient.objects.filter(id__in=ids))


def app_patient_body(spec, registration):
    """Care's patient fields from the app profile. The registration details
    the desk may already hold are kept; the app's fields are laid over them."""
    registration = {
        **registration,
        "prefix": spec.title,
        "first_name": spec.first_name,
        # Care's registration wants 2+ letters; a lone initial stays in the name.
        "last_name": spec.last_name if len(spec.last_name) > 1 else "",
        "email": spec.email,
    }
    if spec.abha_number:
        registration.update(
            abha_number=spec.abha_number,
            abha_status="has",
            identity_storage_consent=True,
        )
    return {
        "name": f"{spec.first_name} {spec.last_name}".strip(),
        "gender": spec.gender,
        "phone_number": spec.phone_number,
        "date_of_birth": spec.date_of_birth.isoformat(),
        "address": spec.address or None,
        "pincode": int(spec.pincode) if spec.pincode else None,
        "registration": registration,
    }


def serialize_app_patient(link):
    patient = link.patient
    return {
        "app_user_id": link.app_user_id,
        "matched": link.matched,
        "uhid": patient_number(patient, PatientNumberSeries.uhid),
        "pre_registration_number": patient_number(
            patient, PatientNumberSeries.pre_registration
        ),
        "patient": PatientRetrieveSpec.serialize(patient).to_json(),
    }


class AppPatientView(EmergencyAPIView):
    """The patient app's server registers a person when they finish their
    profile (docs/app-server.md). Safe to repeat: the app's id finds the
    same patient, whose details are then updated. A new patient gets a PRE-
    reference; the UHID comes on payment or at the first visit. A verified
    ABHA number that belongs to exactly one patient links that patient
    instead, without changing their details."""

    def post(self, request):
        if not request.user.is_service_account:
            raise PermissionDenied("Only the app server registers app profiles")
        spec = AppRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter the person's own mobile number")
        unit_code(facility)

        link = AppPatientLink.objects.select_related("patient").filter(
            app_user_id=spec.app_user_id
        ).first()
        if link:
            check_service_patient(request.user, link.patient)
            if not link.matched:
                with transaction.atomic():
                    patient = link.patient
                    care_view(
                        PatientViewSet,
                        request,
                        "update",
                        external_id=str(patient.external_id),
                    ).handle_update(
                        patient,
                        app_patient_body(
                            spec, (patient.meta or {}).get("registration") or {}
                        ),
                    )
                link.patient.refresh_from_db()
            return Response(serialize_app_patient(link))

        try:
            with transaction.atomic():
                matches = abha_matches(spec.abha_number) if spec.abha_number else []
                if len(matches) == 1:
                    patient = matches[0]
                else:
                    data = care_view(PatientViewSet, request, "create").handle_create(
                        app_patient_body(spec, {})
                    )
                    patient = Patient.objects.get(external_id=data["id"])
                    issue_patient_number(
                        patient, facility, PatientNumberSeries.pre_registration
                    )
                link = AppPatientLink.objects.create(
                    app_user_id=spec.app_user_id,
                    patient=patient,
                    matched=len(matches) == 1,
                    created_by=request.user,
                    updated_by=request.user,
                )
                link_app_patient(request.user, patient)
        except IntegrityError as e:
            # Two calls for the same new profile at once; the other one won.
            raise ValidationError("This profile is already being registered") from e
        return Response(serialize_app_patient(link), status=status.HTTP_201_CREATED)


class AppPatientDetailView(EmergencyAPIView):
    def get(self, request, app_user_id):
        link = get_object_or_404(
            AppPatientLink.objects.select_related("patient"), app_user_id=app_user_id
        )
        check_service_patient(request.user, link.patient)
        if not can("can_view_patient_obj", request.user, link.patient):
            raise PermissionDenied("You do not have permission to view this patient")
        return Response(serialize_app_patient(link))
