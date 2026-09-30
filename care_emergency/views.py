from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.models import Encounter, Patient
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

from care_emergency.constants import TRIAGE_PRIORITY
from care_emergency.models import EmergencyDesk, EmergencyEncounter
from care_emergency.serializers import serialize_desk, serialize_emergency
from care_emergency.services import allocate_mlc_number
from care_emergency.specs import (
    DeskWriteSpec,
    EmergencyRegisterSpec,
    EmergencyUpdateSpec,
    MedicoLegalMixin,
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

        with transaction.atomic():
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                {**spec.patient, "phone_number": desk.phone_number}
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
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
            records.update(
                converted_at=care_now(),
                converted_by=request.user,
                is_unidentified=False,
                updated_by=request.user,
            )
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
