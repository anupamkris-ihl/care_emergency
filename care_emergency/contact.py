"""A shared desk number satisfies Care's required phone, but is not a patient contact."""

from care.security.authorization.base import AuthorizationController
from care.security.authorization.patient import PatientAccess
from care.emr.models.organization import FacilityOrganizationUser
from django.db.models import Q

from care_emergency.models import EmergencyDesk, PatientRegistration


def is_desk_phone(phone):
    return (
        EmergencyDesk.objects.filter(phone_number=phone).exists()
        or PatientRegistration.objects.filter(
            mobile_owner="No phone", patient__phone_number=phone
        ).exists()
    )


def patient_phone_allowed(phone):
    return not is_desk_phone(phone)


def hide_no_phone(mapping, patient, user):
    if PatientRegistration.objects.filter(
        patient=patient, mobile_owner="No phone"
    ).exists():
        mapping["phone_number"] = ""


# Desk registration is the facility association before the first encounter.
# Clinical permissions continue to use Care's encounter-based authorization.


class DeskPatientAccess(PatientAccess):
    def can_register_patient_at_facility_obj(self, user, facility):
        return self.check_permission_in_facility_organization(
            ["can_create_patient"], user, facility=facility
        ) or (
            bool(facility.geo_organization_cache)
            and self.check_permission_in_organization(
                ["can_create_patient"], user, orgs=facility.geo_organization_cache
            )
        )

    def desk_permission(self, user, patient, permission):
        record = (
            PatientRegistration.objects.filter(patient=patient)
            .select_related("facility")
            .first()
        )
        return bool(
            record
            and record.facility
            and self.check_permission_in_facility_organization(
                [permission], user, facility=record.facility
            )
        )

    def can_view_patient_obj(self, user, patient):
        return super().can_view_patient_obj(user, patient) or self.desk_permission(
            user, patient, "can_list_patients"
        )

    def can_write_patient_obj(self, user, patient):
        return super().can_write_patient_obj(user, patient) or self.desk_permission(
            user, patient, "can_write_patient"
        )

    def get_filtered_patients(self, qs, user):
        original = super().get_filtered_patients(qs, user)
        facilities = FacilityOrganizationUser.objects.filter(
            user=user, role_id__in=self.get_role_from_permissions(["can_list_patients"])
        ).values("organization__facility_id")
        patients = PatientRegistration.objects.filter(
            facility_id__in=facilities
        ).values("patient_id")
        return qs.filter(Q(pk__in=original.values("pk")) | Q(pk__in=patients))


AuthorizationController.register_override_controller(DeskPatientAccess)
