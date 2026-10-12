from django.db import models


class TriageChoices(models.TextChoices):
    red = "red", "Red - Immediate"
    yellow = "yellow", "Yellow - Urgent"
    green = "green", "Green - Delayed"
    black = "black", "Black - Deceased on Arrival"


# Care's Encounter.priority has no "deceased on arrival" value, so Black keeps
# the encounter at emergency priority and the triage itself lives on the plug.
TRIAGE_PRIORITY = {
    TriageChoices.red: "emergency",
    TriageChoices.yellow: "urgent",
    TriageChoices.green: "routine",
    TriageChoices.black: "emergency",
}

MLC_NATURES = [
    "Road accident",
    "Assault",
    "Burn",
    "Poisoning",
    "Suicide attempt",
    "Bite",
    "Sexual assault",
    "Fall",
]


# Front-desk registration (POH Front Desk v11 prototype, v12.1 rules). Stored as
# plain text columns; the specs check each value against these lists.
REGISTRATION_TYPES = ["DOM", "TPA", "CORP", "STAFF", "GOVT", "INT", "NRI"]
INTERNATIONAL_TYPES = ["INT", "NRI"]
TITLES = ["Mr", "Master", "Ms", "Mrs", "Dr", "Mx"]
MARITAL_STATUSES = ["married", "single", "divorced", "widowed", "separated"]
GUARDIAN_RELATIONSHIPS = ["S/O", "D/O", "Ward of"]
MOBILE_OWNERS = ["Self", "Parent", "Guardian", "Relative"]
NATIONALITY_CATEGORIES = ["Indian resident", "NRI", "OCI-PIO", "Foreign", "Refugee"]
IDENTITY_CARD_TYPES = [
    "aadhaar",
    "pan",
    "passport",
    "voter_id",
    "driving_license",
    "nrega",
    "birth_certificate",
    "oci_card",
    "unhcr_card",
]
ABHA_STATUSES = ["has", "create", "declined", "not_now"]
VISA_TYPES = [
    "Medical",
    "Medical attendant",
    "e-Medical",
    "Tourist",
    "Business",
    "Employment",
    "Student",
    "Not required",
]
FRRO_C_FORM = [
    "Auto-file within 24 h",
    "Already filed by hotel",
    "Not required (OCI / Nepal / Bhutan)",
]
ALLERGY_STATUSES = ["nka", "has", "unable"]
ALLERGY_SEVERITIES = ["mild", "moderate", "severe", "life_threatening"]
ASSISTANCE_NEEDS = [
    "Wheelchair",
    "Stretcher",
    "Hearing impairment",
    "Visual impairment",
    "Speech impairment",
    "Attendant required",
    "Pacemaker / implant",
]
PREGNANCY_STATUSES = ["not_pregnant", "pregnant", "unknown"]
PAYER_TYPES = [
    "Self",
    "Corporate",
    "Insurance/TPA",
    "Govt scheme",
    "Staff",
    "International",
]
CONSENT_GIVERS = ["The patient", "Spouse", "Parent", "Guardian", "Next of kin"]
CONSENT_METHODS = ["Signature", "Scanned form", "Thumb impression"]
COMMUNICATION_CHANNELS = ["WhatsApp", "SMS", "Phone call", "Email"]
PROMOTION_PURPOSES = [
    "Health tips",
    "Newsletters",
    "Camps",
    "Offers & promotions",
    "News & updates",
]


# Appointments (v12.1 D46, D48, D49, D51, I04). Care's TokenBooking stores only
# patient, note and tags, so these live on the plug's AppointmentDetails.
class ConsultationTypes(models.TextChoices):
    in_person = "in_person", "In person"
    video = "video", "Video consultation"
    home_visit = "home_visit", "Home visit"


# Copied to Encounter.encounter_class when the booking is checked in.
CONSULTATION_ENCOUNTER_CLASS = {
    ConsultationTypes.in_person: "amb",
    ConsultationTypes.video: "vr",
    ConsultationTypes.home_visit: "hh",
}


class VisitPurposes(models.TextChoices):
    new = "new", "New consultation"
    follow_up = "follow_up", "Follow-up"
    report_review = "report_review", "Report review"
    procedure_review = "procedure_review", "Procedure review"


class BookingChannels(models.TextChoices):
    walk_in = "walk_in", "Walk-in"
    front_desk = "front_desk", "Front desk"
    phone = "phone", "Phone"
    online = "online", "Online"


# Phone bookings get an SMS this long before the slot starts.
REMINDER_LEAD_TIME_HOURS = 2


# Patient numbers (v12.1 identifier scheme). Each series is a Care identifier
# config the plug maintains, so staff can't edit the values.
class PatientNumberSeries(models.TextChoices):
    uhid = "POH", "UHID"
    pre_registration = "PRE", "Pre-registration reference"
    unknown = "UNK", "Unknown patient ID"


PATIENT_NUMBER_SYSTEMS = {
    PatientNumberSeries.uhid: "care_emergency/uhid",
    PatientNumberSeries.pre_registration: "care_emergency/pre-registration",
    PatientNumberSeries.unknown: "care_emergency/unknown-patient",
}

# Unit codes are printed in every number, e.g. LN in POH-LN-0010294.
UNIT_CODE_PATTERN = r"^[A-Z]{2,4}$"
