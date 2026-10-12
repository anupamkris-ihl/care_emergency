import datetime

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.api.viewsets.scheduling.availability import SlotViewSet
from care.emr.models import Encounter, Patient
from care.emr.models.scheduling.booking import TokenSlot
from care.emr.models.scheduling.schedule import Availability
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.emr.resources.scheduling.slot.spec import TokenBookingReadSpec
from care.facility.models import Facility
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404
from care.utils.time_util import care_now
from django.db import transaction
from django_filters import rest_framework as filters
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from care_emergency.constants import (
    REMINDER_LEAD_TIME_HOURS,
    TRIAGE_PRIORITY,
    BookingChannels,
    PatientNumberSeries,
)
from care_emergency.models import (
    AppointmentDetails,
    EmergencyDesk,
    EmergencyEncounter,
    FacilityUnit,
    PatientRegistration,
    RegistrationAllergy,
    WalkInReserve,
)
from care_emergency.serializers import (
    serialize_appointment,
    serialize_desk,
    serialize_emergency,
    serialize_registration,
    serialize_unit,
    serialize_walk_in_reserve,
)
from care_emergency.services import (
    allocate_appointment_number,
    allocate_mlc_number,
    allocate_patient_number,
    attach_patient_number,
    check_walk_in_reserve,
    default_visit_purpose,
    issue_patient_number,
    unit_code,
)
from care_emergency.specs import (
    AppointmentBookSpec,
    DeskRegisterSpec,
    DeskWriteSpec,
    EmergencyRegisterSpec,
    EmergencyUpdateSpec,
    MedicoLegalMixin,
    PatientNumberIssueSpec,
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


class RegistrationView(EmergencyAPIView):
    """Front-desk registration: creates the Care patient and its
    PatientRegistration row in one transaction."""

    def post(self, request):
        spec = DeskRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = spec.registration.model_dump(exclude={"allergies"})
        # Phone numbers are optional; the columns keep "" for none.
        details = {
            key: "" if value is None and key.endswith(("_number", "_phone")) else value
            for key, value in details.items()
        }

        if facility:
            unit_code(facility)

        with transaction.atomic():
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                spec.patient
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            if facility:
                issue_patient_number(
                    patient,
                    facility,
                    PatientNumberSeries.pre_registration
                    if spec.pre_registration
                    else PatientNumberSeries.uhid,
                )
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

        return Response(
            {"patient": patient_data, "registration": serialize_registration(record)}
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
        return Response(serialize_registration(record))


class AppointmentFilters(filters.FilterSet):
    patient = filters.UUIDFilter(field_name="booking__patient__external_id")
    appointment_number = filters.CharFilter(
        field_name="appointment_number", lookup_expr="iexact"
    )
    date = filters.DateFilter(field_name="booking__token_slot__start_datetime__date")
    booking_channel = filters.CharFilter(field_name="booking_channel")


class AppointmentViewSet(GenericViewSet):
    """Books through Care's own slot booking and adds the desk details in the
    same transaction. Looked up by the Care booking id."""

    lookup_field = "booking__external_id"
    lookup_url_kwarg = "booking_id"
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = AppointmentFilters

    def get_exception_handler(self):
        return emr_exception_handler

    def get_queryset(self):
        qs = AppointmentDetails.objects.select_related(
            "booking", "booking__token_slot__resource", "facility", "created_by"
        ).order_by("-created_date")
        if self.action == "list":
            facility = get_facility(self.request.query_params.get("facility"))
            if not can("can_list_booking_on_facility", self.request.user, facility):
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
            reminder_due_at = None
            if spec.booking_channel == BookingChannels.phone:
                due = slot.start_datetime - datetime.timedelta(
                    hours=REMINDER_LEAD_TIME_HOURS
                )
                reminder_due_at = due if due > care_now() else None
            details = AppointmentDetails.objects.create(
                booking=booking,
                facility=slot.resource.facility,
                appointment_number=allocate_appointment_number(),
                consultation_type=spec.consultation_type,
                visit_purpose=spec.visit_purpose
                or default_visit_purpose(slot, booking.patient),
                booking_channel=spec.booking_channel,
                reminder_due_at=reminder_due_at,
                created_by=request.user,
                updated_by=request.user,
            )

        return Response(
            {
                "booking": TokenBookingReadSpec.serialize(booking).to_json(),
                "details": serialize_appointment(details),
            }
        )


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
