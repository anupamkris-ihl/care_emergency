"""Statutory confidentiality (v12.1 D30, Zones sheet "Alias").

Once a clinician raises a statutory flag, the patient is shown as their alias
(PT-K7M3QA) in every Care response that carries the patient, through Care's
patient serialization hook. Only the clinicians who raised a flag and users
with an unexpired break-glass access see the identity.
"""

import secrets

from django.utils import timezone

from care_emergency.constants import ALIAS_ALPHABET
from care_emergency.models import BreakGlassAccess, RestrictedPatient, StatutoryFlag

MASKED = "Restricted"

# Identity and contact fields of Care's patient that are hidden.
MASKED_PATIENT_FIELDS = {
    "phone_number": MASKED,
    "emergency_phone_number": None,
    "address": MASKED,
    "permanent_address": None,
    "pincode": None,
    "date_of_birth": None,
    "year_of_birth": None,
    "registration": None,
}


def new_alias():
    while True:
        alias = "PT-" + "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(6))
        if not RestrictedPatient.objects.filter(alias=alias).exists():
            return alias


def restriction_of(patient):
    return RestrictedPatient.objects.filter(patient=patient).first()


def can_see_identity(user, patient):
    """The flagging clinicians and anyone with live break-glass access."""
    if user is None:
        return False
    if StatutoryFlag.objects.filter(patient=patient, created_by=user).exists():
        return True
    return BreakGlassAccess.objects.filter(
        patient=patient, user=user, expires_at__gt=timezone.now()
    ).exists()


def _request_cache():
    """Per-request memo so a patient list costs one query, not one per row."""
    from care.emr.resources.patient.hooks import current_request

    request = current_request()
    if request is None:
        return {}
    if not hasattr(request, "_care_emergency_restricted"):
        request._care_emergency_restricted = {}  # noqa: SLF001
    return request._care_emergency_restricted  # noqa: SLF001


def mask_patient(mapping, patient, user):
    """Care patient serialization hook. Outside a request (tasks, commands)
    nothing is masked, so reminders and exports keep working."""
    if user is None:
        return
    cache = _request_cache()
    if "aliases" not in cache:
        cache["aliases"] = dict(
            RestrictedPatient.objects.values_list("patient_id", "alias")
        )
        cache["visible"] = {}
    alias = cache["aliases"].get(patient.id)
    if alias is None:
        return
    if patient.id not in cache["visible"]:
        cache["visible"][patient.id] = can_see_identity(user, patient)
    if cache["visible"][patient.id]:
        return
    mapping["name"] = alias
    for field, value in MASKED_PATIENT_FIELDS.items():
        if field in mapping:
            mapping[field] = value
