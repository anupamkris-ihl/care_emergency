"""Merging a duplicate patient into the record that is kept (KB "Duplicate &
merge policy").

The source is the duplicate: its visits, bills, documents and other records
move to the target, the demographics staff chose are copied across, and the
source is soft-deleted. Every merge is a PatientMerge row with a snapshot of
the source, so it can be reversed by hand.

FIELDS is the one list of mergeable demographics, shared by the preview and
the merge. Each entry is a group of columns that must stay consistent (the
name and its parts, a phone number and whose it is), so a choice applies to
the whole group.
"""

import datetime
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from care.emr.models import Account, Encounter, FileUpload, Observation
from care.emr.models.patient import Patient, PatientIdentifier
from care.emr.models.questionnaire import QuestionnaireResponse
from care.emr.resources.account.spec import (
    AccountBillingStatusOptions,
    AccountStatusOptions,
)
from care.emr.resources.patient.hooks import run_patient_serialization_hooks
from care.security.authorization import AuthorizationController
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Max, UniqueConstraint
from rest_framework.exceptions import ValidationError

from care_emergency.billing_rules import is_supervisor
from care_emergency.models import (
    ArtCouple,
    EmergencyDesk,
    EmergencyEncounter,
    NewbornBirth,
    PatientMerge,
    PatientRegistration,
    QuickRegistration,
    RegistrationAllergy,
    RestrictedPatient,
)
from care_emergency.services import display_numbers

SOURCE = "source"
TARGET = "target"


# Columns and field groups


@dataclass(frozen=True)
class Column:
    # "patient" (a Patient column), "meta" (a Patient.meta key),
    # "registration" (a PatientRegistration column) or "allergies" (its rows).
    kind: str
    name: str
    # Shown before the value, or alone for a true boolean.
    label: str = ""


def P(name, label=""):  # noqa: N802
    return Column("patient", name, label)


def R(name, label=""):  # noqa: N802
    return Column("registration", name, label)


@dataclass(frozen=True)
class MergeField:
    key: str
    label: str
    columns: tuple[Column, ...]
    # values (column -> value) -> display string; the default joins them all.
    display: Callable | None = None
    # (patient, registration) -> None. Makes the registration columns agree
    # with the patient columns when they come from different records.
    derive: Callable | None = None

    @property
    def patient_columns(self):
        return [c for c in self.columns if c.kind in ("patient", "meta")]

    @property
    def registration_columns(self):
        return [c for c in self.columns if c.kind in ("registration", "allergies")]


def is_empty(value):
    return value is None or value is False or value in ("", 0) or value == []


def format_value(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return ", ".join(format_value(x) for x in value)
    return str(value).strip()


def join_values(values):
    parts = []
    for column, value in values.items():
        if is_empty(value):
            continue
        if value is True:
            parts.append(column.label or column.name)
        elif column.label:
            parts.append(f"{column.label}: {format_value(value)}")
        else:
            parts.append(format_value(value))
    return "; ".join(parts)


def first_value(values):
    return format_value(next(iter(values.values())) or "")


def show_date_of_birth(values):
    dob, year, approximate = values.values()
    if dob:
        shown = dob.strftime("%d %b %Y")
        return f"{shown} (estimated)" if approximate else shown
    return str(year) if year else ""


def show_blood_group(values):
    group = first_value(values)
    if group == "unknown":
        return "Unknown"
    return group.replace("_positive", "+").replace("_negative", "-")


def show_phone(values):
    phone, owner = values.values()
    if not phone:
        return ""
    if owner == "No phone" or EmergencyDesk.objects.filter(phone_number=phone).exists():
        return "No phone"
    return phone


def show_address(values):
    address, pincode = list(values.values())[:2]
    if not address:
        return ""
    return f"{address.strip()}, PIN {pincode}" if pincode else address.strip()


def show_allergies(values):
    status, allergies = values.values()
    if not allergies:
        return status or ""
    return "; ".join(
        f"{substance} ({', '.join(x for x in (reaction, severity) if x)})"
        for substance, reaction, severity in allergies
    )


def split_name(patient, registration):
    parts = patient.name.split()
    registration.first_name = parts[0][:40] if parts else ""
    registration.middle_name = " ".join(parts[1:-1])[:40]
    registration.last_name = parts[-1][:40] if len(parts) > 1 else ""


def phone_owner(patient, registration):
    if EmergencyDesk.objects.filter(phone_number=patient.phone_number).exists():
        registration.mobile_owner = "No phone"
    elif registration.mobile_owner == "No phone":
        registration.mobile_owner = "Self"


def clear_registration(columns):
    def derive(patient, registration):
        for column in columns:
            field = PatientRegistration._meta.get_field(column.name)  # noqa: SLF001
            setattr(registration, column.name, field.get_default())

    return derive


ADDRESS_PARTS = tuple(
    R(name) for name in ("house", "street", "locality", "city", "district", "state")
)
PERMANENT_ADDRESS_PARTS = tuple(
    R(f"permanent_{name}")
    for name in (
        "house",
        "street",
        "locality",
        "city",
        "district",
        "state",
        "pincode",
        "country",
    )
)

FIELDS = [
    MergeField(
        "name",
        "Name",
        (P("name"), R("title"), R("first_name"), R("middle_name"), R("last_name")),
        display=first_value,
        derive=split_name,
    ),
    MergeField("gender", "Gender", (P("gender"),), display=lambda v: first_value(v).capitalize()),
    MergeField(
        "date_of_birth",
        "Date of birth",
        (
            P("date_of_birth"),
            P("year_of_birth"),
            Column("meta", "date_of_birth_approximate"),
        ),
        display=show_date_of_birth,
    ),
    MergeField(
        "phone_number",
        "Mobile number",
        (P("phone_number"), R("mobile_owner", "Belongs to")),
        display=show_phone,
        derive=phone_owner,
    ),
    MergeField(
        "emergency_phone_number", "Emergency phone", (P("emergency_phone_number"),)
    ),
    MergeField(
        "address",
        "Current address",
        (P("address"), P("pincode"), P("geo_organization_id"), *ADDRESS_PARTS),
        display=show_address,
        derive=clear_registration(ADDRESS_PARTS),
    ),
    MergeField(
        "permanent_address",
        "Permanent address",
        (P("permanent_address"), *PERMANENT_ADDRESS_PARTS),
        display=lambda v: first_value(v) or join_values(v),
        derive=clear_registration(PERMANENT_ADDRESS_PARTS),
    ),
    MergeField("blood_group", "Blood group", (P("blood_group"),), display=show_blood_group),
    MergeField("deceased_datetime", "Date of death", (P("deceased_datetime"),)),
    MergeField(
        "nationality",
        "Nationality and travel documents",
        (
            R("registration_type", "Type"),
            R("nationality_category"),
            R("nationality", "Nationality"),
            R("passport_number", "Passport"),
            R("passport_expiry", "Passport expiry"),
            R("visa_type", "Visa"),
            R("visa_expiry", "Visa expiry"),
            R("frro_c_form", "FRRO C-Form"),
            R("address_in_india", "Address in India"),
        ),
    ),
    MergeField("marital_status", "Marital status", (R("marital_status"),)),
    MergeField("father_name", "Father's name", (R("father_name"),)),
    MergeField("mother_name", "Mother's name", (R("mother_name"),)),
    MergeField(
        "guardian",
        "Guardian",
        (R("guardian_name"), R("guardian_relationship", "Relationship")),
    ),
    MergeField("occupation", "Occupation", (R("occupation"),)),
    MergeField("religion", "Religion", (R("religion"),)),
    MergeField("preferred_language", "Preferred language", (R("preferred_language"),)),
    MergeField(
        "interpreter_language", "Interpreter language", (R("interpreter_language"),)
    ),
    MergeField(
        "whatsapp_number",
        "WhatsApp number",
        (R("whatsapp_number"), R("whatsapp_owner", "Belongs to")),
    ),
    MergeField(
        "alternate_phone_number", "Alternate phone", (R("alternate_phone_number"),)
    ),
    MergeField("email", "Email", (R("email"),)),
    MergeField(
        "next_of_kin",
        "Next of kin",
        (
            R("next_of_kin_title"),
            R("next_of_kin_name"),
            R("next_of_kin_relationship", "Relationship"),
            R("next_of_kin_phone", "Phone"),
        ),
    ),
    MergeField(
        "identity_document",
        "Identity document",
        (
            R("identity_card_type"),
            R("identity_number_last4", "Ending"),
            R("identity_storage_consent", "Consent to store"),
        ),
    ),
    MergeField(
        "abha",
        "ABHA",
        (
            R("abha_status", "Status"),
            R("abha_number", "Number"),
            R("abha_address", "Address"),
        ),
    ),
    MergeField(
        "allergies",
        "Allergies",
        (R("allergy_status"), Column("allergies", "allergies")),
        display=show_allergies,
    ),
    MergeField("assistance_needs", "Assistance needs", (R("assistance_needs"),)),
    MergeField("pregnancy_status", "Pregnancy status", (R("pregnancy_status"),)),
    MergeField(
        "vip",
        "VIP handling",
        (
            R("is_vip", "VIP"),
            R("handle_with_care", "Handle with care"),
            R("vip_note", "Note"),
            R("vip_authorised_by", "Authorised by"),
            R("vip_flagged_by_id", "Flagged by user"),
        ),
    ),
    MergeField(
        "payer",
        "Payer",
        (
            R("payer_type"),
            R("payer_name"),
            R("staff_category", "Staff category"),
            R("employee_id", "Employee ID"),
            R("policy_number", "Policy"),
            R("policy_valid_till", "Valid till"),
            R("policy_holder_relationship", "Policy holder"),
        ),
    ),
    MergeField(
        "concession",
        "Concession",
        (R("concession"), R("concession_approved_by", "Approved by")),
    ),
    MergeField(
        "arrival_source",
        "Arrival and lead source",
        (
            R("arrival_source"),
            R("lead_source", "Lead"),
            R("camp_name", "Camp"),
            R("camp_patient_id", "Camp patient ID"),
        ),
    ),
    MergeField(
        "referral",
        "Referral",
        (
            R("referring_provider"),
            R("referrer_registration_number", "Registration number"),
            R("referrer_organisation", "Organisation"),
        ),
    ),
    MergeField("remarks", "Remarks", (R("remarks"),)),
    MergeField(
        "consent",
        "Consent",
        (
            R("consent_given_by", "Given by"),
            R("consent_method", "Method"),
            R("consent_witness", "Witness"),
            R("consent_language", "Language"),
            R("general_consent", "General"),
            R("data_processing_consent", "Data processing"),
            R("tariff_consent", "Tariff"),
            R("share_with_referrer_consent", "Share with referrer"),
            R("abdm_link_consent", "ABDM linking"),
            R("teleconsultation_consent", "Teleconsultation"),
            R("clinical_photography_consent", "Clinical photography"),
            R("research_consent", "Research"),
        ),
    ),
    MergeField(
        "communication",
        "Communication preferences",
        (
            R("communication_channels", "Channels"),
            R("do_not_contact", "Do not contact"),
            R("promotions_consent", "Promotions"),
            R("promotion_purposes", "Promotion topics"),
        ),
    ),
]
FIELD_KEYS = {x.key for x in FIELDS}


def read(column, patient, registration):
    if column.kind == "patient":
        return getattr(patient, column.name)
    if column.kind == "meta":
        return (patient.meta or {}).get(column.name)
    if registration is None:
        return None
    if column.kind == "allergies":
        return sorted(
            registration.allergies.values_list("substance", "reaction", "severity")
        )
    return getattr(registration, column.name)


def write(column, value, patient, registration):
    if column.kind == "patient":
        setattr(patient, column.name, value)
    elif column.kind == "meta":
        patient.meta = dict(patient.meta or {})
        if value is None:
            patient.meta.pop(column.name, None)
        else:
            patient.meta[column.name] = value
    elif column.kind == "allergies":
        registration.allergies.all().delete()
        RegistrationAllergy.objects.bulk_create(
            RegistrationAllergy(
                registration=registration,
                substance=substance,
                reaction=reaction,
                severity=severity,
            )
            for substance, reaction, severity in value or []
        )
    else:
        setattr(registration, column.name, list(value) if isinstance(value, list) else value)


@dataclass
class Comparison:
    field: MergeField
    source: dict
    target: dict
    source_display: str
    target_display: str

    @property
    def conflict(self):
        return bool(
            self.source_display
            and self.target_display
            and self.source_display != self.target_display
        )

    def to_json(self):
        return {
            "key": self.field.key,
            "label": self.field.label,
            "source": self.source_display,
            "target": self.target_display,
            "conflict": self.conflict,
        }


def display(field, values):
    shown = field.display(values) if field.display else ""
    return shown or join_values(values)


def compare(source, target):
    source_registration = registration_of(source)
    target_registration = registration_of(target)
    comparisons = []
    for field in FIELDS:
        s = {c: read(c, source, source_registration) for c in field.columns}
        t = {c: read(c, target, target_registration) for c in field.columns}
        comparisons.append(Comparison(field, s, t, display(field, s), display(field, t)))
    return comparisons


def registration_of(patient):
    return PatientRegistration.objects.filter(patient=patient).first()


# Related records


# Plug relations handled by name rather than by the generic move.
SPECIAL_RELATIONS = {
    (PatientIdentifier, "patient"),
    (PatientRegistration, "patient"),
    (QuickRegistration, "patient"),
    (ArtCouple, "woman"),
    (ArtCouple, "partner"),
    (RestrictedPatient, "patient"),
    (PatientMerge, "source"),
    (PatientMerge, "target"),
}

MOVE_LABELS = {
    "emr.encounter": "Visits",
    "emr.tokenbooking": "Appointments",
    "emr.token": "Queue tokens",
    "emr.account": "Billing accounts",
    "emr.invoice": "Invoices",
    "emr.chargeitem": "Charge items",
    "emr.patientidentifier": "Identifiers",
    "emr.patientorganization": "Organisations",
    "emr.patientuser": "Linked users",
    "care_emergency.patientdocument": "Documents",
    "care_emergency.emergencyencounter": "Emergency records",
    "care_emergency.patientregistration": "Desk registration",
    "care_emergency.quickregistration": "Quick registration",
    "care_emergency.artcouple": "IVF couple record",
    "care_emergency.newbornbirth": "Birth record",
    "care_emergency.statutoryflag": "Statutory flags",
}


def move_label(model):
    label = model._meta.label_lower  # noqa: SLF001
    return MOVE_LABELS.get(
        label, str(model._meta.verbose_name_plural).capitalize()  # noqa: SLF001
    )


def patient_relations():
    """Every relation pointing at Patient, including hidden ones
    (related_name="+"), so nothing is left behind on the source."""
    for relation in Patient._meta.get_fields(include_hidden=True):  # noqa: SLF001
        if not (relation.auto_created and not relation.concrete):
            continue
        if not (relation.one_to_many or relation.one_to_one):
            continue
        yield relation.related_model, relation.field


def generic_relations():
    for model, field in patient_relations():
        if (model, field.name) not in SPECIAL_RELATIONS:
            yield model, field


def live(model):
    return model._default_manager  # noqa: SLF001


def every(model):
    return model._base_manager  # noqa: SLF001


def identifier_plan(source, target):
    """(identifiers to move, identifiers to drop): the target keeps its own
    value for every identifier config it has, patient numbers included, so it
    ends with one UHID. The source's other identifiers move."""
    kept = set(
        PatientIdentifier.objects.filter(patient=target).values_list(
            "config_id", flat=True
        )
    )
    move, drop = [], []
    for identifier in PatientIdentifier.objects.filter(patient=source):
        (drop if identifier.config_id in kept else move).append(identifier)
    return move, drop


def patient_files(patient):
    return FileUpload.objects.filter(
        file_type="patient", associating_id=str(patient.external_id)
    )


def planned_moves(source, target):
    """Label -> number of the source's records that move, non-zero only."""
    moves = defaultdict(int)
    for model, field in generic_relations():
        count = live(model).filter(**{field.name: source}).count()
        if count:
            moves[move_label(model)] += count
    move, _ = identifier_plan(source, target)
    if move:
        moves[move_label(PatientIdentifier)] += len(move)
    files = patient_files(source).count()
    if files:
        moves["Files"] += files
    if registration_of(source) and not every(PatientRegistration).filter(
        patient=target
    ).exists():
        moves[move_label(PatientRegistration)] += 1
    if QuickRegistration.objects.filter(patient=source).exists() and not every(
        QuickRegistration
    ).filter(patient=target).exists():
        moves[move_label(QuickRegistration)] += 1
    couples = ArtCouple.objects.filter(woman=source).count() + ArtCouple.objects.filter(
        partner=source
    ).count()
    if couples:
        moves[move_label(ArtCouple)] += couples
    return dict(moves)


# Blockers and permission


def is_unconverted_emergency(patient):
    return EmergencyEncounter.objects.filter(
        patient=patient, converted_at__isnull=True, is_unidentified=True
    ).exists()


def unique_collision(model, field, source, target):
    """Whether moving the source's rows would break one of the model's unique
    constraints that include the patient."""
    groups = [
        (constraint.fields, constraint.condition)
        for constraint in model._meta.constraints  # noqa: SLF001
        if isinstance(constraint, UniqueConstraint)
        and field.name in (constraint.fields or ())
    ]
    groups += [
        (fields, None)
        for fields in model._meta.unique_together  # noqa: SLF001
        if field.name in fields
    ]
    for fields, condition in groups:
        others = [x for x in fields if x != field.name]
        rows = every(model).all()
        if condition is not None:
            rows = rows.filter(condition)
        if not others:
            if (
                rows.filter(**{field.name: source}).exists()
                and rows.filter(**{field.name: target}).exists()
            ):
                return True
            continue
        present = {f"{x}__isnull": False for x in others}
        theirs = set(rows.filter(**{field.name: source}, **present).values_list(*others))
        ours = set(rows.filter(**{field.name: target}, **present).values_list(*others))
        if theirs & ours:
            return True
    return False


def merge_blockers(source, target):  # noqa: C901
    """Human-readable reasons the merge can't go ahead; empty when it can."""
    if source.id == target.id:
        return ["Choose two different records"]
    blockers = []
    if RestrictedPatient.objects.filter(patient__in=[source, target]).exists():
        blockers.append("Restricted records are merged by a privileged process")
    for patient, role in ((source, "duplicate"), (target, "kept")):
        if is_unconverted_emergency(patient):
            blockers.append(
                f"Complete the {role} record's emergency registration before merging"
            )
    if ArtCouple.objects.filter(woman=source, partner=target).exists() or (
        ArtCouple.objects.filter(woman=target, partner=source).exists()
    ):
        blockers.append("These records are the two partners of an IVF couple")
    elif NewbornBirth.objects.filter(mother=source, baby=target).exists() or (
        NewbornBirth.objects.filter(mother=target, baby=source).exists()
    ):
        blockers.append("These records are a mother and her baby")
    else:
        for field, label in (("woman", "the woman"), ("partner", "the partner")):
            if (
                every(ArtCouple).filter(**{field: source}).exists()
                and every(ArtCouple).filter(**{field: target}).exists()
            ):
                blockers.append(f"Both records are {label} of an IVF couple")
    open_accounts = Account.objects.filter(
        status=AccountStatusOptions.active.value,
        billing_status=AccountBillingStatusOptions.open.value,
    )
    shared = set(
        open_accounts.filter(patient=source).values_list("facility__name", "facility_id")
    ) & set(
        open_accounts.filter(patient=target).values_list("facility__name", "facility_id")
    )
    for facility_name, _ in sorted(shared):
        blockers.append(
            f"Both records have an open billing account at {facility_name}. "
            "Close one of them first"
        )
    # A removed registration still holds the one-to-one, so the source's
    # registration couldn't move to the kept record.
    if (
        PatientRegistration.objects.filter(patient=source).exists()
        and not PatientRegistration.objects.filter(patient=target).exists()
        and every(PatientRegistration).filter(patient=target).exists()
    ):
        blockers.append("The kept record has a removed desk registration")
    for model, field in generic_relations():
        name = model._meta.verbose_name  # noqa: SLF001
        if field.one_to_one:
            if (
                every(model).filter(**{field.name: source}).exists()
                and every(model).filter(**{field.name: target}).exists()
            ):
                blockers.append(f"Both records have a {name}")
        elif unique_collision(model, field, source, target):
            blockers.append(
                f"Both records have {model._meta.verbose_name_plural} that "  # noqa: SLF001
                "can't be combined"
            )
    return blockers


def merge_facility(source, target):
    """Where merge authority is checked: the kept record's registration
    facility, or the duplicate's when only it has one (its registration then
    moves to the kept record)."""
    for patient in (target, source):
        registration = registration_of(patient)
        if registration and registration.facility_id:
            return registration.facility
    return None


def can_merge(user, source, target):
    """Desk supervisor and above: write access to both records and the
    supervisor role (Facility Admin or Admin) at the merge facility.
    Superusers always."""
    if not all(
        AuthorizationController.call("can_write_patient_obj", user, patient)
        for patient in (source, target)
    ):
        return False
    if user.is_superuser:
        return True
    facility = merge_facility(source, target)
    return facility is not None and is_supervisor(user, facility)


# Preview


def last_visits(patient_ids):
    """Patient id -> created date of their latest visit, in one query."""
    return dict(
        Encounter.objects.filter(patient_id__in=patient_ids)
        .order_by()
        .values("patient_id")
        .annotate(last=Max("created_date"))
        .values_list("patient_id", "last")
    )


def side(patient, numbers, visits):
    mapping = {
        "id": str(patient.external_id),
        "name": patient.name,
        "gender": patient.gender,
        "phone_number": patient.phone_number,
        "date_of_birth": patient.date_of_birth,
        "year_of_birth": patient.year_of_birth,
        "patient_number": numbers.get(patient.id),
        "created_date": patient.created_date,
        "last_visit": visits.get(patient.id),
    }
    # The same masking (restricted, no phone) as everywhere else.
    run_patient_serialization_hooks(mapping, patient)
    return mapping


def merge_preview(source, target):
    ids = [source.id, target.id]
    numbers = display_numbers(ids)
    visits = last_visits(ids)
    blockers = merge_blockers(source, target)
    restricted = RestrictedPatient.objects.filter(patient__in=ids).exists()
    fields = []
    if source.id != target.id and not restricted:
        fields = [
            x.to_json()
            for x in compare(source, target)
            if x.source_display or x.target_display
        ]
    return {
        "source": side(source, numbers, visits),
        "target": side(target, numbers, visits),
        "fields": fields,
        "moves": planned_moves(source, target) if source.id != target.id else {},
        "blockers": blockers,
    }


# Merge


def validation_errors(errors):
    """The emr_exception_handler shape, one entry per problem."""
    return ValidationError({"errors": errors})


def check_merge(source, target, choices):
    """Raises the blockers, unknown keys and missing choices as a 400."""
    blockers = merge_blockers(source, target)
    if blockers:
        raise validation_errors(
            [{"type": "merge_blocked", "loc": ["blockers"], "msg": x} for x in blockers]
        )
    errors = [
        {"type": "unknown_field", "loc": ["choices", key], "msg": f"Unknown field {key}"}
        for key in sorted(set(choices) - FIELD_KEYS)
    ]
    comparisons = compare(source, target)
    errors += [
        {
            "type": "choice_required",
            "loc": ["choices", x.field.key],
            "msg": f"Choose which {x.field.label.lower()} to keep",
        }
        for x in comparisons
        if x.conflict and x.field.key not in choices
    ]
    if errors:
        raise validation_errors(errors)
    return comparisons


def to_json(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def dump(objects):
    return to_json(serializers.serialize("python", objects))


def snapshot(source, target, source_registration, target_registration, moved_ids):
    """Enough to reverse the merge by hand: both records as they were, the
    source's identifiers and the ids of every row that moved."""
    registrations = [x for x in (source_registration,) if x]
    return {
        "patient": dump([source]),
        "registration": dump(registrations),
        "allergies": dump(
            RegistrationAllergy.objects.filter(registration__in=registrations)
        ),
        "quick_registration": dump(QuickRegistration.objects.filter(patient=source)),
        "identifiers": [
            {
                "id": x.id,
                "system": (x.config.config or {}).get("system"),
                "config": str(x.config.external_id),
                "value": x.value,
            }
            for x in PatientIdentifier.objects.filter(patient=source).select_related(
                "config"
            )
        ],
        "files": list(patient_files(source).values_list("id", flat=True)),
        "target_before": {
            "patient": dump([target]),
            "registration": dump([target_registration] if target_registration else []),
            "allergies": dump(
                RegistrationAllergy.objects.filter(registration=target_registration)
                if target_registration
                else []
            ),
        },
        "moved_ids": moved_ids,
    }


def apply_fields(comparisons, choices, target, target_registration, source_registration):  # noqa: C901
    """Copies the chosen and missing demographics onto the target. Returns the
    registration the target ends with (its own, the source's moved one, or
    None)."""
    moved_registration = target_registration is None and source_registration is not None
    registration = target_registration or source_registration
    for comparison in comparisons:
        field = comparison.field
        if comparison.conflict:
            if choices[field.key] == SOURCE:
                for column in field.patient_columns:
                    write(column, comparison.source[column], target, registration)
                if field.registration_columns and registration is not None:
                    if source_registration is None:
                        if field.derive:
                            field.derive(target, registration)
                    elif not moved_registration:
                        for column in field.registration_columns:
                            write(column, comparison.source[column], target, registration)
            elif moved_registration and field.registration_columns and field.derive:
                # The kept record's value with the duplicate's registration.
                field.derive(target, registration)
            continue
        for column in field.columns:
            if column.kind in ("registration", "allergies") and (
                registration is None or moved_registration
            ):
                continue
            if is_empty(comparison.target[column]) and not is_empty(
                comparison.source[column]
            ):
                write(column, comparison.source[column], target, registration)
    return registration, moved_registration


def merge_tags(source, target):
    target.instance_tags = sorted({*target.instance_tags, *source.instance_tags})
    facility_tags = dict(target.facility_tags or {})
    for facility_id, tags in (source.facility_tags or {}).items():
        facility_tags[facility_id] = sorted({*facility_tags.get(facility_id, []), *tags})
    target.facility_tags = facility_tags


def move_records(source, target, moved_ids):  # noqa: C901
    """Re-points everything of the source's onto the target."""
    for model, field in generic_relations():
        rows = every(model).filter(**{field.name: source})
        ids = list(rows.values_list("pk", flat=True))
        if ids:
            moved_ids[model._meta.label_lower + "." + field.name] = ids  # noqa: SLF001
            rows.update(**{field.name: target})
    # Questionnaire responses and observations also name their subject by id.
    for model in (Observation, QuestionnaireResponse):
        every(model).filter(patient=target, subject_id=source.external_id).update(
            subject_id=target.external_id
        )
    patient_files(source).update(associating_id=str(target.external_id))

    move, drop = identifier_plan(source, target)
    facilities = {x.config.facility_id for x in [*move, *drop] if x.config.facility_id}
    PatientIdentifier.objects.filter(id__in=[x.id for x in move]).update(patient=target)
    # Dropped (soft-deleted) identifiers stay on the source, so an old UHID
    # still leads to the kept record through the PatientMerge row.
    PatientIdentifier.objects.filter(id__in=[x.id for x in drop]).update(deleted=True)

    quick = QuickRegistration.objects.filter(patient=source).first()
    if quick:
        if every(QuickRegistration).filter(patient=target).exists():
            QuickRegistration.objects.filter(id=quick.id).update(deleted=True)
        else:
            QuickRegistration.objects.filter(id=quick.id).update(patient=target)
    ArtCouple.objects.filter(woman=source).update(woman=target)
    ArtCouple.objects.filter(partner=source).update(partner=target)
    return facilities


def merge_patients(source, target, choices, reason, user):  # noqa: PLR0913
    """Merges source into target; both must be locked by the caller. Returns
    the PatientMerge row."""
    comparisons = check_merge(source, target, choices)
    source_registration = registration_of(source)
    target_registration = registration_of(target)
    moved = planned_moves(source, target)
    moved_ids = {}
    record_snapshot = snapshot(
        source, target, source_registration, target_registration, moved_ids
    )

    registration, moved_registration = apply_fields(
        comparisons, choices, target, target_registration, source_registration
    )
    merge_tags(source, target)
    facilities = move_records(source, target, moved_ids)
    record_snapshot["moved_ids"] = to_json(moved_ids)

    target.updated_by = user
    target.save()
    if registration is not None:
        if moved_registration:
            registration.patient = target
        registration.updated_by = user
        # Saved, not updated, so the payer tags follow (signals).
        registration.save()
    if source_registration is not None and not moved_registration:
        PatientRegistration.objects.filter(id=source_registration.id).update(
            deleted=True
        )

    target.refresh_from_db()
    target.build_instance_identifiers()
    for facility_id in facilities:
        target.build_facility_identifiers(str(facility_id))
    target.save(update_fields=["instance_identifiers", "facility_identifiers"])

    source.meta = {**(source.meta or {}), "merged_into": str(target.external_id)}
    source.deleted = True
    source.updated_by = user
    source.save(update_fields=["deleted", "meta", "updated_by", "modified_date"])

    return PatientMerge.objects.create(
        source=source,
        target=target,
        reason=reason,
        choices={x.field.key: choices[x.field.key] for x in comparisons if x.conflict},
        source_snapshot=record_snapshot,
        moved=moved,
        created_by=user,
        updated_by=user,
    )


def resolve_merged(patient_ids, hops=10):
    """Patient ids, with each merged-away patient replaced by the record it
    was merged into (following later merges of that record too)."""
    resolved = set()
    pending = set(patient_ids)
    for _ in range(hops):
        merges = dict(
            PatientMerge.objects.filter(source_id__in=pending).values_list(
                "source_id", "target_id"
            )
        )
        resolved |= pending - set(merges)
        pending = set(merges.values())
        if not pending:
            break
    return resolved | pending
