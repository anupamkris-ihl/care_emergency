"""The patient app's server: its service account, its two roles and what it
may touch (docs/app-server.md)."""

from care.emr.models.organization import FacilityOrganizationUser
from care.emr.models.patient import PatientUser
from care.facility.models import Facility
from care.security.models import PermissionModel, RoleModel, RolePermission
from care.users.models import User
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from care_emergency.constants import (
    APP_PATIENT_PERMISSIONS,
    APP_PATIENT_ROLE,
    APP_SERVER_PERMISSIONS,
    APP_SERVER_ROLE,
    APP_SERVER_USERNAME,
)


def sync_role(name, description, permissions):
    """Creates the role, or brings its permissions back to exactly the list.
    Not a system role, so Care's sync_permissions_roles leaves it alone."""
    role = RoleModel.objects.filter(name=name).first()
    if role is None:
        role = RoleModel.objects.create(
            name=name, description=description, contexts=["FACILITY"]
        )
    wanted = PermissionModel.objects.filter(slug__in=permissions)
    missing = set(permissions) - set(wanted.values_list("slug", flat=True))
    if missing:
        err = f"Run sync_permissions_roles first; missing {', '.join(sorted(missing))}"
        raise RuntimeError(err)
    RolePermission.objects.filter(role=role).exclude(permission__in=wanted).delete()
    for permission in wanted:
        RolePermission.objects.get_or_create(role=role, permission=permission)
    return role


def app_server_user():
    return User.objects.filter(username=APP_SERVER_USERNAME, deleted=False).first()


def app_patient_role():
    return RoleModel.objects.get(name=APP_PATIENT_ROLE)


def grant_facility(user, role, organization_id):
    """The app-server role in a facility's root organization, which covers
    every department and doctor in it."""
    if organization_id is None:
        return False
    _, created = FacilityOrganizationUser.objects.get_or_create(
        organization_id=organization_id, user=user, defaults={"role": role}
    )
    return created


def grant_new_facility(organization):
    """A facility created after setup gets the app server too (signals)."""
    user = app_server_user()
    role = RoleModel.objects.filter(name=APP_SERVER_ROLE).first()
    if user and role:
        grant_facility(user, role, organization.id)


@transaction.atomic
def setup_app_server():
    """Idempotent: the roles, the service account and its role at every
    facility. Returns (user, number of facilities newly granted)."""
    role = sync_role(
        APP_SERVER_ROLE,
        "The patient app's server: registers patients and books at a facility",
        APP_SERVER_PERMISSIONS,
    )
    sync_role(
        APP_PATIENT_ROLE,
        "The patient app's server, on a patient it registered",
        APP_PATIENT_PERMISSIONS,
    )
    user = app_server_user()
    if user is None:
        user = User(
            username=APP_SERVER_USERNAME,
            first_name="Patient app",
            last_name="server",
            user_type="staff",
            is_service_account=True,
        )
        # Signs in with an API key only.
        user.set_unusable_password()
        user.save()
    granted = sum(
        grant_facility(user, role, facility.default_internal_organization_id)
        for facility in Facility.objects.filter(deleted=False)
    )
    return user, granted


def link_app_patient(user, patient):
    PatientUser.objects.get_or_create(
        patient=patient, user=user, defaults={"role": app_patient_role()}
    )


def is_linked(user, patient):
    return PatientUser.objects.filter(patient=patient, user=user).exists()


def check_service_patient(user, patient):
    """A service account (the app server) may only act for patients linked to
    it; staff are checked by Care's own permissions."""
    if user.is_service_account and not is_linked(user, patient):
        raise PermissionDenied("This patient was not registered through the app")


def linked_patients(user):
    return PatientUser.objects.filter(user=user).values("patient_id")
