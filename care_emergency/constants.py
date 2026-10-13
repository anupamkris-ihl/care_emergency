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

# Emergency quick registration (v11 §7.1). Stored as plain text columns.
CHIEF_COMPLAINTS = [
    "Chest pain",
    "Breathlessness",
    "Trauma - road traffic",
    "Fall",
    "Burns",
    "Poisoning",
    "Assault",
    "Seizure",
    "Unconscious",
    "Bleeding",
    "Labour",
    "Other",
]

ARRIVAL_MODES = [
    "Walked in",
    "POH ambulance",
    "108 ambulance",
    "Police",
    "Transfer from another hospital",
]


# Front-desk registration (POH Front Desk v11 prototype, v12.1 rules). Stored as
# plain text columns; the specs check each value against these lists.
REGISTRATION_TYPES = ["DOM", "TPA", "CORP", "STAFF", "GOVT", "INT", "NRI", "CPL"]
INTERNATIONAL_TYPES = ["INT", "NRI"]
TITLES = ["Mr", "Master", "Ms", "Mrs", "Dr", "Mx"]
MARITAL_STATUSES = ["married", "single", "divorced", "widowed", "separated"]
GUARDIAN_RELATIONSHIPS = ["S/O", "D/O", "Ward of"]
# Who a phone number belongs to (main mobile and WhatsApp). "Parent" and
# "Relative" stay valid for main mobiles registered before the full list.
PHONE_OWNERS = [
    "Self",
    "Father",
    "Mother",
    "Spouse",
    "Son",
    "Daughter",
    "Brother",
    "Sister",
    "Guardian",
    "Friend",
    "Other",
]
MOBILE_OWNERS = [*PHONE_OWNERS, "Parent", "Relative", "No phone"]
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
STAFF_CATEGORIES = [
    "Doctor",
    "Nurse",
    "Paramedical",
    "Administration",
    "Support staff",
    "Other",
]
# Staff payer names: the employee themselves, or a family member they cover.
STAFF_SELF = "POH staff — self"
STAFF_DEPENDANT = "POH staff — dependant"
STAFF_PAYERS = [STAFF_SELF, STAFF_DEPENDANT]
PAYER_TYPES = [
    "Self",
    "Corporate",
    "Insurance/TPA",
    "Govt scheme",
    "Staff",
    "International",
]
RELIGIONS = [
    "Hindu",
    "Muslim",
    "Christian",
    "Sikh",
    "Buddhist",
    "Jain",
    "Parsi",
    "Jewish",
    "Other",
    "Prefer not to say",
]
# v12.1 M06: the 22 Eighth Schedule languages, English and foreign languages.
EIGHTH_SCHEDULE_LANGUAGES = [
    "Assamese",
    "Bengali",
    "Bodo",
    "Dogri",
    "Gujarati",
    "Hindi",
    "Kannada",
    "Kashmiri",
    "Konkani",
    "Maithili",
    "Malayalam",
    "Manipuri",
    "Marathi",
    "Nepali",
    "Odia",
    "Punjabi",
    "Sanskrit",
    "Santali",
    "Sindhi",
    "Tamil",
    "Telugu",
    "Urdu",
]
FOREIGN_LANGUAGES = [
    "Arabic",
    "Dari",
    "French",
    "Pashto",
    "Russian",
    "Swahili",
    "Uzbek",
    "Other",
]
LANGUAGES = ["English", *EIGHTH_SCHEDULE_LANGUAGES, *FOREIGN_LANGUAGES]
CONSENT_GIVERS = ["The patient", "Spouse", "Parent", "Guardian", "Next of kin"]
CONSENT_METHODS = ["Signature", "Scanned form", "Thumb impression"]
COMMUNICATION_CHANNELS = ["WhatsApp", "SMS", "Phone call", "IVR", "Email"]
PROMOTION_PURPOSES = [
    "Health tips",
    "Newsletters",
    "Camps",
    "Offers & promotions",
    "News & updates",
]

# Counter-entered sources only. Digital arrival must come from its own flow,
# never from a receptionist choosing it during desk registration (D38/R28).
DESK_ARRIVAL_SOURCES = [
    "Walk-in",
    "Our own doctor",
    "Outside doctor",
    "Another hospital",
    "Health camp",
    "Corporate tie-up",
    "Call centre",
    "Insurance network",
    "Ambulance / police",
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


# Front-desk documents (v12.1 D37, D43). The file is a Care FileUpload of the
# patient; the plug records what it is, which side and how it was captured.
class DocumentCategories(models.TextChoices):
    government_id = "government_id", "Government ID"
    payer_card = "payer_card", "Payer card"
    passport = "passport", "Passport"
    visa = "visa", "Visa"
    marriage_certificate = "marriage_certificate", "Marriage certificate"
    previous_prescription = "previous_prescription", "Previous prescription"
    referral_letter = "referral_letter", "Referral letter"
    other = "other", "Other"


class DocumentSides(models.TextChoices):
    front = "front", "Front"
    back = "back", "Back"
    single = "single", "Single page"


class CaptureMethods(models.TextChoices):
    photographed = "photographed", "Photographed"
    scanned = "scanned", "Scanned"
    uploaded = "uploaded", "Uploaded"


# Care's own categories for the FileUpload behind each document.
DOCUMENT_FILE_CATEGORY = {
    DocumentCategories.government_id: "identity_proof",
    DocumentCategories.passport: "identity_proof",
    DocumentCategories.visa: "identity_proof",
}
DOCUMENT_MIME_TYPES = ["image/jpeg", "image/png", "image/webp", "application/pdf"]


# IVF / ART couples (v12.1 D45, R51, R52; ART Act 2021 s.21(g)).
ART_WOMAN_AGES = (21, 50)
ART_MAN_AGES = (21, 55)


# "I don't know whom to meet" (v12.1 D50). A red flag sends the patient to
# emergency instead of a booking.
GUIDANCE_AREAS = [
    "Heart / chest",
    "Breathing / lungs",
    "Stomach / digestion",
    "Bones / joints / back",
    "Brain / nerves / headache",
    "Skin / hair",
    "Ear / nose / throat",
    "Eyes",
    "Kidney / urine",
    "Women's health / pregnancy",
    "Child's health",
    "Mind / mood / sleep",
    "Teeth / mouth",
    "Fever / general weakness",
    "Not sure",
]
GUIDANCE_SINCE = [
    "Today",
    "1-3 days",
    "4-7 days",
    "1-4 weeks",
    "More than a month",
]
GUIDANCE_RED_FLAGS = [
    "Chest pain or pressure",
    "Breathless at rest",
    "Sudden weakness, face droop or slurred speech",
    "Fainting or unconsciousness",
    "Fits / seizure",
    "Heavy bleeding",
    "Severe injury or burn",
    "Thoughts of self-harm",
]


# Statutory confidentiality (v12.1 D30, R32; MTP, POCSO, HIV, MHCA). Raised by
# the treating clinician, never at the counter; masks the patient everywhere.
class StatutoryCategories(models.TextChoices):
    mtp = "mtp", "MTP"
    pocso = "pocso", "POCSO"
    hiv = "hiv", "HIV"
    mental_health = "mental_health", "Mental health"
    sexual_assault = "sexual_assault", "Sexual assault"
    substance_use = "substance_use", "Substance use"


# PT- + 6 characters without the look-alikes 0, O, 1 and I.
ALIAS_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
BREAK_GLASS_HOURS = 1

# Quick registration (phone booking): how long the full-form link stays open.
QUICK_FORM_LINK_DAYS = 7
