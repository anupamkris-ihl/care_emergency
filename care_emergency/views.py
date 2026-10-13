import datetime
import re
import secrets

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.api.viewsets.file_upload import file_authorizer
from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.api.viewsets.scheduling.availability import SlotViewSet
from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Encounter, FacilityOrganization, Patient
from care.emr.models.organization import FacilityOrganizationUser
from care.emr.models.patient import PatientIdentifier
from care.emr.models.scheduling.booking import TokenSlot
from care.emr.models.scheduling.schedule import Availability
from care.emr.resources.encounter.constants import COMPLETED_CHOICES
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.emr.resources.scheduling.slot.spec import TokenBookingReadSpec
from care.facility.models import Facility
from care.security.authorization import AuthorizationController
from care.users.models import User
from care.utils.shortcuts import get_object_or_404
from care.utils.sms import send_text_message
from care.utils.time_util import care_now
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from care_emergency.constants import (
    BREAK_GLASS_HOURS,
    QUICK_FORM_LINK_DAYS,
    DOCUMENT_MIME_TYPES,
    REMINDER_LEAD_TIME_HOURS,
    TRIAGE_PRIORITY,
    BookingChannels,
    PatientNumberSeries,
)
from care_emergency.models import (
    AppointmentDetails,
    ArtCouple,
    BreakGlassAccess,
    EmergencyDesk,
    EmergencyEncounter,
    FacilityUnit,
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
    issue_patient_number,
    missing_documents,
    staged_documents,
    store_document,
    unit_code,
)
from care_emergency.specs import (
    AppointmentBookSpec,
    BreakGlassSpec,
    DeskRegisterSpec,
    DeskContactSpec,
    RegistrationSpec,
    DeskWriteSpec,
    DocumentUploadSpec,
    EmergencyRegisterSpec,
    EmergencyUpdateSpec,
    MedicoLegalMixin,
    PatientNumberIssueSpec,
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


class RegistrationView(EmergencyAPIView):
    """Front-desk registration: creates the Care patient, its
    PatientRegistration row, an IVF partner and the desk documents in one
    transaction."""

    def post(self, request):
        spec = DeskRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = spec.registration.model_dump(exclude={"allergies"})
        # Phone numbers are optional; the columns keep "" for none.
        details = {
            key: "" if value is None and key.endswith(("_number", "_phone")) else value
            for key, value in details.items()
        }
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
        ids = PatientIdentifier.objects.filter(
            config__config__system="care_emergency/uhid", value=value
        ).values("patient_id")
        results = [
            PatientRetrieveSpec.serialize(patient).to_json()
            for patient in Patient.objects.filter(id__in=ids)
            if can("can_view_patient_obj", request.user, patient)
        ]
        return Response({"count": len(results), "results": results})


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
