from care_emergency.constants import CONSULTATION_ENCOUNTER_CLASS


def serialize_desk(desk):
    return {
        "facility": str(desk.facility.external_id),
        "phone_number": desk.phone_number,
        "modified_date": desk.modified_date,
    }


def serialize_unit(unit):
    return {
        "facility": str(unit.facility.external_id),
        "code": unit.code,
        "modified_date": unit.modified_date,
    }


def serialize_emergency(record):
    return {
        "id": str(record.external_id),
        "encounter": str(record.encounter.external_id),
        "encounter_status": record.encounter.status,
        "patient": {
            "id": str(record.patient.external_id),
            "name": record.patient.name,
            "gender": record.patient.gender,
            "year_of_birth": record.patient.year_of_birth,
        },
        "facility": str(record.facility.external_id),
        "triage": record.triage,
        "is_medico_legal": record.is_medico_legal,
        "mlc_nature": record.mlc_nature,
        "police_station": record.police_station,
        "mlc_number": record.mlc_number,
        "distinguishing_marks": record.distinguishing_marks,
        "brought_by": {
            "name": record.brought_by_name,
            "phone_number": record.brought_by_phone,
            "relationship": record.brought_by_relationship,
        },
        "is_unidentified": record.is_unidentified,
        "converted_at": record.converted_at,
        "created_date": record.created_date,
        "created_by": record.created_by.username if record.created_by else None,
    }


REGISTRATION_FIELDS = [
    "registration_type",
    "title",
    "first_name",
    "middle_name",
    "last_name",
    "marital_status",
    "father_name",
    "mother_name",
    "guardian_name",
    "guardian_relationship",
    "occupation",
    "religion",
    "preferred_language",
    "mobile_owner",
    "whatsapp_number",
    "alternate_phone_number",
    "email",
    "house",
    "street",
    "locality",
    "city",
    "district",
    "state",
    "next_of_kin_name",
    "next_of_kin_relationship",
    "next_of_kin_phone",
    "nationality_category",
    "identity_storage_consent",
    "identity_card_type",
    "identity_number_last4",
    "abha_status",
    "abha_number",
    "abha_address",
    "nationality",
    "passport_number",
    "passport_expiry",
    "visa_type",
    "visa_expiry",
    "frro_c_form",
    "address_in_india",
    "interpreter_language",
    "allergy_status",
    "assistance_needs",
    "pregnancy_status",
    "is_vip",
    "vip_note",
    "vip_authorised_by",
    "handle_with_care",
    "payer_type",
    "payer_name",
    "employee_id",
    "policy_number",
    "policy_valid_till",
    "policy_holder_relationship",
    "concession",
    "concession_approved_by",
    "arrival_source",
    "lead_source",
    "camp_name",
    "camp_patient_id",
    "referring_provider",
    "referrer_registration_number",
    "referrer_organisation",
    "remarks",
    "consent_given_by",
    "consent_method",
    "consent_witness",
    "consent_language",
    "general_consent",
    "data_processing_consent",
    "tariff_consent",
    "share_with_referrer_consent",
    "abdm_link_consent",
    "teleconsultation_consent",
    "clinical_photography_consent",
    "research_consent",
    "communication_channels",
    "promotions_consent",
    "promotion_purposes",
]


def serialize_registration(record):
    return {
        "id": str(record.external_id),
        "patient": str(record.patient.external_id),
        "facility": str(record.facility.external_id) if record.facility else None,
        **{field: getattr(record, field) for field in REGISTRATION_FIELDS},
        "allergies": [
            {
                "substance": allergy.substance,
                "reaction": allergy.reaction,
                "severity": allergy.severity,
            }
            for allergy in record.allergies.all()
        ],
        "vip_flagged_by": record.vip_flagged_by.username
        if record.vip_flagged_by
        else None,
        "created_date": record.created_date,
        "created_by": record.created_by.username if record.created_by else None,
    }


def serialize_appointment(details):
    return {
        "id": str(details.external_id),
        "booking": str(details.booking.external_id),
        "facility": str(details.facility.external_id),
        "appointment_number": details.appointment_number,
        "consultation_type": details.consultation_type,
        "encounter_class": CONSULTATION_ENCOUNTER_CLASS[details.consultation_type],
        "visit_purpose": details.visit_purpose,
        "booking_channel": details.booking_channel,
        "reminder_due_at": details.reminder_due_at,
        "reminder_sent_at": details.reminder_sent_at,
        "created_date": details.created_date,
        "created_by": details.created_by.username if details.created_by else None,
    }


def serialize_walk_in_reserve(reserve):
    return {
        "availability": str(reserve.availability.external_id),
        "walk_in_every": reserve.walk_in_every,
        "modified_date": reserve.modified_date,
    }
