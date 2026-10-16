"""Newborn registration: what a baby takes from the mother's desk
registration, and what it deliberately doesn't (clinical record, allergies,
blood group, government ID). Intake suite: Newborn "Baby of…" registration."""

from care_emergency.constants import STAFF_DEPENDANT

# Copied unchanged from the mother's PatientRegistration: address, contact,
# next of kin, nationality, payer and the consents that cover the newborn.
INHERITED_FIELDS = [
    "preferred_language",
    "whatsapp_number",
    "alternate_phone_number",
    "email",
    "house",
    "street",
    "locality",
    "city",
    "district",
    "state",
    "permanent_house",
    "permanent_street",
    "permanent_locality",
    "permanent_city",
    "permanent_district",
    "permanent_state",
    "permanent_pincode",
    "permanent_country",
    "next_of_kin_title",
    "next_of_kin_name",
    "next_of_kin_relationship",
    "next_of_kin_phone",
    "nationality_category",
    "nationality",
    "address_in_india",
    "interpreter_language",
    "payer_type",
    "payer_name",
    "staff_category",
    "employee_id",
    "policy_number",
    "policy_valid_till",
    "consent_method",
    "consent_witness",
    "consent_language",
    "general_consent",
    "data_processing_consent",
    "tariff_consent",
    "international_tariff_consent",
    "communication_channels",
    "do_not_contact",
]

# Payers billed against a policy holder; the baby is the holder's child.
HOLDER_PAYERS = ("Corporate", "Insurance/TPA", "Govt scheme", "Staff")

# A number the mother owns is the baby's mother's; her spouse's is the father's.
OWNER_FOR_BABY = {"Self": "Mother", "Spouse": "Father"}

TITLES = {"male": "Master", "female": "Ms"}
GUARDIAN_RELATIONS = {"male": "S/O", "female": "D/O"}

ARRIVAL_SOURCE = "Born at the facility"


def baby_name(mother_name, birth_order, number_of_babies):
    """ "Baby Of <mother>" for a single baby, "Baby 2 Of <mother>" for twins
    and more. Replaced by the legal name once the family names the child."""
    if number_of_babies == 1:
        return f"Baby Of {mother_name}"
    return f"Baby {birth_order} Of {mother_name}"


def baby_registration(mother, mother_record, gender):
    """PatientRegistration column values for a baby of the mother.
    mother_record is her desk registration, or None when she was registered
    outside the desk; the baby then gets only the defaults below."""
    inherited = (
        {field: getattr(mother_record, field) for field in INHERITED_FIELDS}
        if mother_record
        else {"preferred_language": "English", "nationality_category": "Indian resident"}
    )
    registration_type = mother_record.registration_type if mother_record else "DOM"
    if registration_type == "CPL":
        # The IVF couple type belongs to the mother's own treatment.
        registration_type = "DOM"
    mobile_owner = mother_record.mobile_owner if mother_record else "Mother"
    whatsapp_owner = mother_record.whatsapp_owner if mother_record else ""
    payer_type = inherited.get("payer_type") or "Self"
    payer_name = inherited.get("payer_name", "")
    if payer_type == "Staff":
        payer_name = STAFF_DEPENDANT
    return {
        **inherited,
        "registration_type": registration_type,
        "title": TITLES.get(gender, "Mx"),
        "first_name": "Baby",
        "mother_name": mother.name[:100],
        "guardian_name": mother.name[:40],
        "guardian_relationship": GUARDIAN_RELATIONS.get(gender, "Ward of"),
        "mobile_owner": OWNER_FOR_BABY.get(mobile_owner, mobile_owner) or "Mother",
        "whatsapp_owner": OWNER_FOR_BABY.get(whatsapp_owner, whatsapp_owner),
        "payer_type": payer_type,
        "payer_name": payer_name,
        "policy_holder_relationship": "Child" if payer_type in HOLDER_PAYERS else "",
        "allergy_status": "",
        "arrival_source": ARRIVAL_SOURCE,
        "consent_given_by": "Parent",
        "consent_method": inherited.get("consent_method", ""),
        "consent_language": inherited.get("consent_language", ""),
    }


def serialize_birth(birth):
    return {
        "id": str(birth.external_id),
        "baby": str(birth.baby.external_id),
        "mother": str(birth.mother.external_id),
        "facility": str(birth.facility.external_id),
        "born_at": birth.born_at,
        "birth_order": birth.birth_order,
        "number_of_babies": birth.number_of_babies,
        "delivery_type": birth.delivery_type,
        "gestation_weeks": birth.gestation_weeks,
        "birth_weight_grams": birth.birth_weight_grams,
        "length_cm": float(birth.length_cm) if birth.length_cm is not None else None,
        "head_circumference_cm": float(birth.head_circumference_cm)
        if birth.head_circumference_cm is not None
        else None,
        "apgar_1_min": birth.apgar_1_min,
        "apgar_5_min": birth.apgar_5_min,
    }
