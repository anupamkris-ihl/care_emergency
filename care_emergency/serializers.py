def serialize_desk(desk):
    return {
        "facility": str(desk.facility.external_id),
        "phone_number": desk.phone_number,
        "modified_date": desk.modified_date,
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
