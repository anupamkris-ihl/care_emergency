from django.utils import timezone

from care_emergency.constants import CONSULTATION_ENCOUNTER_CLASS
from care_emergency.models import ArtCouple, BreakGlassAccess, NewbornBirth
from care_emergency.newborn import serialize_birth


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
        "chief_complaint": record.chief_complaint,
        "mode_of_arrival": record.mode_of_arrival,
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
    "whatsapp_owner",
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
    "staff_category",
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
    "do_not_contact",
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
        "couple": serialize_couple(record.patient),
        "birth": serialize_newborn(record.patient),
        "restricted": False,
        "created_date": record.created_date,
        "created_by": record.created_by.username if record.created_by else None,
    }


def serialize_restricted_registration(record, restriction):
    """What the desk sees of a restricted patient (D30): the alias, never the
    identity, contact or the reason."""
    return {
        "id": str(record.external_id),
        "patient": str(record.patient.external_id),
        "facility": str(record.facility.external_id) if record.facility else None,
        "registration_type": record.registration_type,
        "restricted": True,
        "alias": restriction.alias,
        "created_date": record.created_date,
    }


def serialize_newborn(patient):
    birth = (
        NewbornBirth.objects.filter(baby=patient)
        .select_related("baby", "mother", "facility")
        .first()
    )
    return serialize_birth(birth) if birth else None


def serialize_couple(patient):
    couple = (
        ArtCouple.objects.filter(woman=patient).select_related("partner").first()
        or ArtCouple.objects.filter(partner=patient).select_related("woman").first()
    )
    if couple is None:
        return None
    return {
        "woman": str(couple.woman.external_id),
        "partner": str(couple.partner.external_id) if couple.partner else None,
        "single_woman": couple.single_woman,
        "art_consent": couple.art_consent,
        "partner_identity_card_type": couple.partner_identity_card_type,
        "partner_identity_number_last4": couple.partner_identity_number_last4,
        "partner_abha_number": couple.partner_abha_number,
        "partner_abha_address": couple.partner_abha_address,
    }


def serialize_document(document):
    file = document.file
    return {
        "id": str(document.external_id),
        "patient": str(document.patient.external_id) if document.patient else None,
        "category": document.category,
        "side": document.side,
        "capture_method": document.capture_method,
        "source": document.source,
        "document_date": document.document_date,
        "name": file.name,
        "mime_type": file.meta.get("mime_type"),
        "read_signed_url": file.files_manager.read_signed_url(file)
        if document.patient
        else None,
        "created_date": document.created_date,
        "created_by": document.created_by.username if document.created_by else None,
    }


def serialize_restriction(restriction, user, visible):
    """visible: the user may see the identity, so the alias, categories and
    their own break-glass expiry are shown too."""
    if restriction is None:
        return {"restricted": False}
    data = {"restricted": True, "alias": restriction.alias, "can_see_identity": visible}
    if visible:
        patient = restriction.patient
        data["categories"] = sorted(
            set(patient.statutory_flags.values_list("category", flat=True))
        )
        access = (
            BreakGlassAccess.objects.filter(
                patient=patient, user=user, expires_at__gt=timezone.now()
            )
            .order_by("-expires_at")
            .first()
        )
        data["break_glass_until"] = access.expires_at if access else None
    return data


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
        "guidance_area": details.guidance_area,
        "guidance_since": details.guidance_since,
        "chief_complaint": details.chief_complaint,
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


def serialize_quick_registration(record):
    doctor = record.doctor
    return {
        "id": str(record.external_id),
        "patient": str(record.patient.external_id),
        "facility": str(record.facility.external_id),
        "title": record.title,
        "first_name": record.first_name,
        "last_name": record.last_name,
        "consultation_type": record.consultation_type,
        "department": {
            "id": str(record.department.external_id),
            "name": record.department.name,
        }
        if record.department
        else None,
        "doctor": {
            "id": str(doctor.external_id),
            "name": doctor.get_full_name() or doctor.username,
        }
        if doctor
        else None,
        "whatsapp_same_as_mobile": record.whatsapp_same_as_mobile,
        "send_form_link": record.send_form_link,
        "send_payment_reminder": record.send_payment_reminder,
        "form_expires_at": record.form_expires_at,
        "form_link_sent_at": record.form_link_sent_at,
        "form_submitted_at": record.form_submitted_at,
        "form_details": record.form_details,
        "created_date": record.created_date,
        "created_by": record.created_by.username if record.created_by else None,
    }
