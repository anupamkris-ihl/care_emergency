import datetime
import re

from care.emr.resources.base import PhoneNumber
from django.utils import timezone
from pydantic import UUID4, BaseModel, Field, field_validator, model_validator

from care_emergency.constants import (
    ABHA_STATUSES,
    ALLERGY_SEVERITIES,
    ALLERGY_STATUSES,
    ART_MAN_AGES,
    ART_WOMAN_AGES,
    ASSISTANCE_NEEDS,
    COMMUNICATION_CHANNELS,
    CONSENT_GIVERS,
    CONSENT_METHODS,
    DESK_ARRIVAL_SOURCES,
    FRRO_C_FORM,
    GUARDIAN_RELATIONSHIPS,
    GUIDANCE_AREAS,
    GUIDANCE_RED_FLAGS,
    GUIDANCE_SINCE,
    IDENTITY_CARD_TYPES,
    INTERNATIONAL_TYPES,
    LANGUAGES,
    MARITAL_STATUSES,
    MLC_NATURES,
    MOBILE_OWNERS,
    NATIONALITY_CATEGORIES,
    PAYER_TYPES,
    PHONE_OWNERS,
    PREGNANCY_STATUSES,
    PROMOTION_PURPOSES,
    REGISTRATION_TYPES,
    RELIGIONS,
    STAFF_CATEGORIES,
    TITLES,
    UNIT_CODE_PATTERN,
    VISA_TYPES,
    BookingChannels,
    CaptureMethods,
    ConsultationTypes,
    DocumentCategories,
    DocumentSides,
    StatutoryCategories,
    TriageChoices,
    VisitPurposes,
)
from care_emergency.pincodes import address_problem


class BroughtBySpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_number: PhoneNumber | None = Field(default=None, max_length=14)
    relationship: str = Field(default="", max_length=50)


class DeskWriteSpec(BaseModel):
    facility: UUID4
    phone_number: PhoneNumber = Field(max_length=14)


class UnitWriteSpec(BaseModel):
    facility: UUID4
    code: str = Field(pattern=UNIT_CODE_PATTERN)


class PatientNumberIssueSpec(BaseModel):
    facility: UUID4


class MedicoLegalMixin:
    def check_medico_legal(self, is_medico_legal, mlc_nature, police_station):
        if is_medico_legal:
            if mlc_nature not in MLC_NATURES:
                raise ValueError("Select the MLC nature")
            if not police_station.strip():
                raise ValueError("Enter the police station informed")


class EmergencyRegisterSpec(MedicoLegalMixin, BaseModel):
    facility: UUID4
    # Passed through to Care's PatientCreateSpec; phone_number is replaced by
    # the facility's emergency desk number.
    patient: dict
    organizations: list[UUID4] = []
    triage: TriageChoices
    is_medico_legal: bool
    mlc_nature: str = ""
    police_station: str = Field(default="", max_length=200)
    distinguishing_marks: str = Field(default="", max_length=500)
    is_unidentified: bool = True
    brought_by: BroughtBySpec | None = None

    @model_validator(mode="after")
    def validate_medico_legal(self):
        self.check_medico_legal(
            self.is_medico_legal, self.mlc_nature, self.police_station
        )
        if not self.is_medico_legal:
            self.mlc_nature = ""
            self.police_station = ""
        return self

    @model_validator(mode="after")
    def validate_age(self):
        # v12.1 D05: an estimated age is 0-120, matching Care's patient spec.
        age = self.patient.get("age")
        if self.patient.get("date_of_birth"):
            return self
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 120:  # noqa: PLR2004
            raise ValueError("Enter an estimated age between 0 and 120")
        return self


class EmergencyUpdateSpec(BaseModel):
    triage: TriageChoices | None = None
    is_medico_legal: bool | None = None
    mlc_nature: str | None = None
    police_station: str | None = Field(default=None, max_length=200)
    distinguishing_marks: str | None = Field(default=None, max_length=500)
    brought_by: BroughtBySpec | None = None


NAME_PART = r"(?=.{2,40}\Z)[A-Za-z]+( [A-Za-z]+)*"
FULL_NAME = r"[A-Za-z]+( [A-Za-z]+)*"
PAYER_ID = r"[A-Z0-9/-]{1,24}"
ABHA_ADDRESS = r"[a-z0-9._]{4,}@(abdm|sbx)"
# Only the last 4 characters of a government ID are kept, so the server checks
# them against the tail of each format: Aadhaar 12 digits, PAN ABCDE1234F,
# EPIC ABC1234567, driving licence DL0120110012345, Indian passport A1234567.
ID_TAILS = {
    "aadhaar": r"\d{4}",
    "pan": r"\d{3}[A-Z]",
    "voter_id": r"\d{4}",
    "driving_license": r"\d{4}",
    "passport": r"\d{4}",
}
INDIAN_PASSPORT = r"[A-Z]\d{7}"
# Indian citizens: their passport (and its tail) must be in the Indian format.
INDIAN_PASSPORT_HOLDERS = ("Indian resident", "NRI")


def validate_id_tail(card_type, last4, *, indian=True, whose="ID"):
    tail = ID_TAILS.get(card_type)
    if card_type == "passport" and not indian:
        tail = None
    if last4 and tail and not re.fullmatch(tail, last4):
        err = f"The last 4 characters do not fit the {whose} number's format"
        raise ValueError(err)


def one_of(choices, *, blank=True):
    """A validator that accepts only values from choices ("" too when blank)."""

    def check(value):
        if value == "" and blank:
            return value
        if value not in choices:
            err = f"'{value}' is not one of: {', '.join(choices)}"
            raise ValueError(err)
        return value

    return check


class AllergySpec(BaseModel):
    substance: str = Field(min_length=1, max_length=100)
    reaction: str = Field(min_length=1, max_length=100)
    severity: str

    _severity = field_validator("severity")(one_of(ALLERGY_SEVERITIES, blank=False))


REGISTRATION_CHOICES = {
    "registration_type": (REGISTRATION_TYPES, False),
    "title": (TITLES, False),
    "marital_status": (MARITAL_STATUSES, True),
    "religion": (RELIGIONS, True),
    "preferred_language": (LANGUAGES, False),
    "interpreter_language": (LANGUAGES, True),
    "consent_language": (LANGUAGES, False),
    "guardian_relationship": (GUARDIAN_RELATIONSHIPS, True),
    "mobile_owner": (MOBILE_OWNERS, False),
    "whatsapp_owner": (PHONE_OWNERS, True),
    "nationality_category": (NATIONALITY_CATEGORIES, False),
    "identity_card_type": (IDENTITY_CARD_TYPES, True),
    "abha_status": (ABHA_STATUSES, True),
    "visa_type": (VISA_TYPES, True),
    "frro_c_form": (FRRO_C_FORM, True),
    "allergy_status": (ALLERGY_STATUSES, False),
    "pregnancy_status": (PREGNANCY_STATUSES, True),
    "payer_type": (PAYER_TYPES, False),
    "staff_category": (STAFF_CATEGORIES, True),
    "consent_given_by": (CONSENT_GIVERS, False),
    "consent_method": (CONSENT_METHODS, False),
}


class RegistrationSpec(BaseModel):
    """The front-desk details kept in PatientRegistration. "" means not given."""

    registration_type: str
    title: str
    first_name: str
    middle_name: str = ""
    last_name: str = ""
    marital_status: str = ""
    father_name: str = Field(default="", max_length=100)
    mother_name: str = Field(default="", max_length=100)
    guardian_name: str = ""
    guardian_relationship: str = ""
    occupation: str = Field(default="", max_length=50)
    religion: str = Field(default="", max_length=30)
    preferred_language: str = Field(min_length=1, max_length=30)

    mobile_owner: str
    whatsapp_number: PhoneNumber | None = Field(default=None, max_length=16)
    whatsapp_owner: str = ""
    alternate_phone_number: PhoneNumber | None = Field(default=None, max_length=16)
    email: str = Field(default="", max_length=254)
    house: str = Field(default="", max_length=100)
    street: str = Field(default="", max_length=150)
    locality: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    # Permanent address, when it differs from the current one. Its text line
    # is also sent as the Care patient's permanent_address.
    permanent_house: str = Field(default="", max_length=100)
    permanent_street: str = Field(default="", max_length=150)
    permanent_locality: str = Field(default="", max_length=100)
    permanent_city: str = Field(default="", max_length=100)
    permanent_district: str = Field(default="", max_length=100)
    permanent_state: str = Field(default="", max_length=100)
    permanent_pincode: str = Field(default="", pattern=r"^(?:[1-9]\d{5})?$")
    next_of_kin_name: str = ""
    next_of_kin_relationship: str = Field(default="", max_length=30)
    next_of_kin_phone: PhoneNumber | None = Field(default=None, max_length=16)

    nationality_category: str
    identity_storage_consent: bool = False
    identity_card_type: str = ""
    identity_number_last4: str = Field(default="", pattern=r"^(?:[A-Z0-9]{4})?$")
    abha_status: str = ""
    abha_number: str = Field(default="", pattern=r"^(?:\d{14})?$")
    abha_address: str = Field(default="", max_length=60)

    nationality: str = Field(default="", max_length=60)
    passport_number: str = Field(default="", pattern=r"^(?:[A-Z0-9]{6,15})?$")
    passport_expiry: datetime.date | None = None
    visa_type: str = ""
    visa_expiry: datetime.date | None = None
    frro_c_form: str = ""
    address_in_india: str = Field(default="", max_length=300)
    interpreter_language: str = Field(default="", max_length=30)

    allergy_status: str
    allergies: list[AllergySpec] = Field(default=[], max_length=20)
    assistance_needs: list[str] = []
    pregnancy_status: str = ""

    is_vip: bool = False
    vip_note: str = Field(default="", max_length=200)
    vip_authorised_by: str = Field(default="", max_length=100)
    handle_with_care: bool = False

    payer_type: str
    payer_name: str = Field(default="", max_length=200)
    staff_category: str = ""
    employee_id: str = Field(default="", pattern=rf"^(?:{PAYER_ID})?$")
    policy_number: str = Field(default="", pattern=rf"^(?:{PAYER_ID})?$")
    policy_valid_till: datetime.date | None = None
    policy_holder_relationship: str = Field(default="", max_length=30)
    concession: str = Field(default="", max_length=60)
    concession_approved_by: str = Field(default="", max_length=100)

    arrival_source: str = Field(min_length=1, max_length=40)
    lead_source: str = Field(default="", max_length=60)
    camp_name: str = Field(default="", max_length=100)
    camp_patient_id: str = Field(default="", max_length=30)
    referring_provider: str = Field(default="", max_length=200)
    referrer_registration_number: str = Field(default="", max_length=30)
    referrer_organisation: str = Field(default="", max_length=200)
    remarks: str = Field(default="", max_length=1000)

    consent_given_by: str
    consent_method: str
    consent_witness: str = ""
    consent_language: str = Field(min_length=1, max_length=30)
    general_consent: bool
    data_processing_consent: bool
    tariff_consent: bool
    share_with_referrer_consent: bool = False
    abdm_link_consent: bool = False
    teleconsultation_consent: bool = False
    clinical_photography_consent: bool = False
    research_consent: bool = False
    communication_channels: list[str] = []
    # Explicit opt-out: no channels and no promotions.
    do_not_contact: bool = False
    promotions_consent: bool = False
    promotion_purposes: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def strip_text(cls, data):
        if isinstance(data, dict):
            return {
                key: value.strip() if isinstance(value, str) else value
                for key, value in data.items()
            }
        return data

    @model_validator(mode="after")
    def validate_choices(self):
        for field, (choices, blank) in REGISTRATION_CHOICES.items():
            one_of(choices, blank=blank)(getattr(self, field))
        for value in self.assistance_needs:
            one_of(ASSISTANCE_NEEDS, blank=False)(value)
        for value in self.communication_channels:
            one_of(COMMUNICATION_CHANNELS, blank=False)(value)
        for value in self.promotion_purposes:
            one_of(PROMOTION_PURPOSES, blank=False)(value)
        one_of(DESK_ARRIVAL_SOURCES, blank=False)(self.arrival_source)
        if self.promotion_purposes and not self.promotions_consent:
            raise ValueError("Promotion purposes need promotional consent")
        if self.do_not_contact and (
            self.communication_channels or self.promotions_consent
        ):
            raise ValueError(
                "Do not contact cannot have communication channels or promotions"
            )
        if self.whatsapp_owner and not self.whatsapp_number:
            raise ValueError("Enter the WhatsApp number its owner belongs to")
        if self.staff_category and self.payer_type != "Staff":
            raise ValueError("The staff category is only for staff payers")
        return self

    @model_validator(mode="after")
    def validate_names(self):
        if not re.fullmatch(NAME_PART, self.first_name):
            raise ValueError("The first name must be 2-40 letters with single spaces")
        for label, value in (
            ("middle name", self.middle_name),
            ("last name", self.last_name),
            ("guardian's name", self.guardian_name),
            ("next of kin's name", self.next_of_kin_name),
            ("witness's name", self.consent_witness),
        ):
            if value and not re.fullmatch(NAME_PART, value):
                err = f"The {label} must be 2-40 letters with single spaces"
                raise ValueError(err)
        for label, value in (
            ("father's name", self.father_name),
            ("mother's name", self.mother_name),
        ):
            if value and not re.fullmatch(FULL_NAME, value):
                err = f"The {label} can only have letters and single spaces"
                raise ValueError(err)
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", self.email):
            raise ValueError("Enter a valid email address")
        return self

    @model_validator(mode="after")
    def validate_documents(self):
        today = timezone.localdate()
        international = self.registration_type in INTERNATIONAL_TYPES
        if international == (self.nationality_category == "Indian resident"):
            raise ValueError("The nationality does not match the registration type")
        if self.identity_number_last4 and not self.identity_card_type:
            raise ValueError("Choose the government ID type")
        indian = self.nationality_category in INDIAN_PASSPORT_HOLDERS
        validate_id_tail(
            self.identity_card_type, self.identity_number_last4, indian=indian
        )
        if (
            self.identity_card_type or self.abha_number or self.abha_address
        ) and not self.identity_storage_consent:
            raise ValueError("Consent is required to store the ID summary")
        if self.abha_address and not re.fullmatch(ABHA_ADDRESS, self.abha_address):
            raise ValueError("The ABHA address must look like name@abdm")
        if self.abha_status == "has" and not (self.abha_number or self.abha_address):
            raise ValueError("Enter the ABHA number or address")
        if international:
            if not self.nationality or not self.passport_number:
                raise ValueError("Enter the nationality and passport number")
            if self.nationality_category in ("Foreign", "NRI") and not (
                self.address_in_india
            ):
                raise ValueError("Enter the address in India")
        if (
            indian
            and self.passport_number
            and not re.fullmatch(INDIAN_PASSPORT, self.passport_number)
        ):
            raise ValueError("An Indian passport number looks like A1234567")
        if self.nationality_category == "Foreign":
            if not self.passport_expiry or not self.visa_type or not self.frro_c_form:
                raise ValueError(
                    "Enter the passport expiry, visa and C-Form for a foreign national"
                )
            if self.visa_type != "Not required" and not self.visa_expiry:
                raise ValueError("Enter the visa expiry date")
        for label, value in (
            ("passport", self.passport_expiry),
            ("visa", self.visa_expiry),
            ("payer card or policy", self.policy_valid_till),
        ):
            if value and value < today:
                err = f"The {label} has expired"
                raise ValueError(err)
        return self

    @model_validator(mode="after")
    def validate_flags(self):
        if (self.allergy_status == "has") != bool(self.allergies):
            raise ValueError(
                "List the allergies when, and only when, the patient has allergies"
            )
        if self.is_vip and not (self.vip_note and self.vip_authorised_by):
            raise ValueError("A VIP flag needs a note and who authorised it")
        if self.consent_method == "Thumb impression" and not self.consent_witness:
            raise ValueError("Enter the witness for a thumb-impression consent")
        if not (
            self.general_consent
            and self.data_processing_consent
            and self.tariff_consent
        ):
            raise ValueError(
                "General, DPDP notice and tariff consents are all required"
            )
        return self

    @model_validator(mode="after")
    def validate_payer(self):
        needs_id = self.payer_type in ("Corporate", "Staff")
        needs_policy = self.payer_type in ("Insurance/TPA", "Govt scheme")
        if self.payer_type not in ("Self", "International") and not self.payer_name:
            raise ValueError("Choose the payer")
        if needs_id and not self.employee_id:
            raise ValueError("Enter the employee ID")
        if needs_policy and not self.policy_number:
            raise ValueError("Enter the policy or card number")
        if self.payer_type == "Govt scheme" and not self.occupation:
            raise ValueError("Government schemes need the occupation")
        return self


def years_old(born, today):
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


class PartnerSpec(BaseModel):
    """The male partner of an IVF / ART couple (D45). Registered as his own
    Care patient; his ID summary is kept like the patient's."""

    first_name: str
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: datetime.date
    phone_number: PhoneNumber = Field(max_length=14)
    identity_card_type: str
    identity_number_last4: str = Field(pattern=r"^[A-Z0-9]{4}$")
    abha_number: str = Field(default="", pattern=r"^(?:\d{14})?$")
    abha_address: str = Field(default="", max_length=60)
    # Staged desk documents: at least his government ID.
    documents: list[UUID4] = Field(default=[], max_length=10)

    @model_validator(mode="after")
    def validate_partner(self):
        self.first_name = self.first_name.strip()
        self.middle_name = self.middle_name.strip()
        self.last_name = self.last_name.strip()
        if not re.fullmatch(NAME_PART, self.first_name):
            raise ValueError(
                "The partner's first name must be 2-40 letters with single spaces"
            )
        for value in (self.middle_name, self.last_name):
            if value and not re.fullmatch(NAME_PART, value):
                raise ValueError(
                    "The partner's names must be 2-40 letters with single spaces"
                )
        one_of(IDENTITY_CARD_TYPES, blank=False)(self.identity_card_type)
        validate_id_tail(
            self.identity_card_type, self.identity_number_last4, whose="partner's ID"
        )
        if self.abha_address and not re.fullmatch(ABHA_ADDRESS, self.abha_address):
            raise ValueError("The partner's ABHA address must look like name@abdm")
        return self

    @property
    def name(self):
        parts = (self.first_name, self.middle_name, self.last_name)
        return " ".join(part for part in parts if part)


class CoupleSpec(BaseModel):
    """IVF / ART couple registration (D45, R51, R52). A single woman registers
    without a partner and without a marriage certificate."""

    single_woman: bool = False
    partner: PartnerSpec | None = None
    art_consent: bool

    @model_validator(mode="after")
    def validate_couple(self):
        if self.single_woman == (self.partner is not None):
            raise ValueError(
                "Enter the partner, or mark the registration as a single woman"
            )
        if not self.art_consent:
            raise ValueError("The ART consent is required")
        return self


class DeskRegisterSpec(BaseModel):
    facility: UUID4 | None = None
    # Passed through to Care's PatientCreateSpec.
    patient: dict
    registration: RegistrationSpec
    # Issues a PRE- reference instead of a UHID; the UHID follows on arrival.
    pre_registration: bool = False
    # Staged desk documents (documents/) to attach to the patient.
    documents: list[UUID4] = Field(default=[], max_length=20)
    couple: CoupleSpec | None = None

    @model_validator(mode="after")
    def validate_couple(self):
        today = timezone.localdate()
        couple = self.couple
        if (self.registration.registration_type == "CPL") != (couple is not None):
            raise ValueError("IVF / ART couple details go with the IVF couple type")
        if couple is None:
            return self
        if self.patient.get("gender") != "female":
            raise ValueError("Register the woman of an IVF / ART couple first")
        try:
            born = datetime.date.fromisoformat(self.patient.get("date_of_birth") or "")
        except (TypeError, ValueError) as e:
            raise ValueError("IVF / ART needs the woman's exact date of birth") from e
        low, high = ART_WOMAN_AGES
        if not low <= years_old(born, today) <= high:
            err = f"The woman must be {low}-{high} years old for ART"
            raise ValueError(err)
        if couple.partner:
            low, high = ART_MAN_AGES
            if not low <= years_old(couple.partner.date_of_birth, today) <= high:
                err = f"The partner must be {low}-{high} years old for ART"
                raise ValueError(err)
        if not self.registration.identity_card_type:
            raise ValueError("Capture the woman's government ID for IVF / ART")
        return self

    @model_validator(mode="after")
    def validate_patient(self):
        if self.pre_registration and not self.facility:
            raise ValueError("Pre-registration needs a facility")
        today = timezone.localdate()
        registration = self.registration
        dob = self.patient.get("date_of_birth")
        age = self.patient.get("age")
        if dob:
            try:
                born = datetime.date.fromisoformat(dob)
            except (TypeError, ValueError) as e:
                raise ValueError("Enter the date of birth as YYYY-MM-DD") from e
            if born < datetime.date(1900, 1, 1) or born > today:
                raise ValueError("Date of birth must be between 1900 and today")
            age = (
                today.year
                - born.year
                - ((today.month, today.day) < (born.month, born.day))
            )
        if not isinstance(age, int) or isinstance(age, bool):
            raise ValueError("Enter the date of birth or the age")
        if not 0 <= age <= 120:  # noqa: PLR2004
            raise ValueError("Age must be between 0 and 120")
        minor = age < 18  # noqa: PLR2004
        if minor and not dob:
            raise ValueError("Patients under 18 need an exact date of birth")
        if minor and not (
            registration.guardian_name
            and registration.guardian_relationship
            and registration.next_of_kin_name
            and (
                registration.next_of_kin_phone
                or registration.mobile_owner == "No phone"
            )
        ):
            raise ValueError("Patients under 18 need a guardian and a next of kin")
        if minor and registration.marital_status:
            raise ValueError("Marital status is not recorded for patients under 18")
        if registration.pregnancy_status and not (
            self.patient.get("gender") == "female" and 12 <= age <= 50  # noqa: PLR2004
        ):
            raise ValueError("Pregnancy is recorded only for women aged 12-50")
        phone = self.patient.get("phone_number")
        if phone and registration.alternate_phone_number == phone:
            raise ValueError("The alternate mobile must differ from the main number")
        self.validate_demographics(minor)
        self.validate_contact()
        self.validate_policy_holder(age, minor)
        return self

    def validate_demographics(self, minor):
        registration = self.registration
        gender = self.patient.get("gender")
        titles = {"Mx"}
        if gender == "male":
            titles.add("Master" if minor else "Mr")
        elif gender == "female":
            titles.add("Ms")
            if not minor:
                titles.add("Mrs")
        if not minor:
            titles.add("Dr")
        if registration.title not in titles:
            raise ValueError("The title does not match the patient's gender and age")
        relation = registration.guardian_relationship
        if (relation == "S/O" and gender == "female") or (
            relation == "D/O" and gender == "male"
        ):
            raise ValueError(
                "The guardian relation does not match the patient's gender"
            )
        if minor:
            if registration.occupation and registration.occupation not in (
                "Student",
                "Not applicable — child",
            ):
                raise ValueError(
                    "A minor's occupation must be Student or Not applicable"
                )
            if registration.mobile_owner == "Self":
                raise ValueError(
                    "A minor's mobile must belong to a parent, guardian or relative"
                )
            if registration.consent_given_by not in ("Parent", "Guardian"):
                raise ValueError(
                    "A minor's consent must be given by a parent or guardian"
                )
            if not registration.next_of_kin_relationship:
                raise ValueError("Enter the next of kin's relation")
        domestic = registration.registration_type not in INTERNATIONAL_TYPES
        if domestic and not registration.house:
            raise ValueError("Enter the house, flat or building")
        allowed_ids = {"passport", "oci_card", "unhcr_card"}
        if domestic or registration.nationality_category == "NRI":
            allowed_ids = {
                "aadhaar",
                "pan",
                "passport",
                "voter_id",
                "driving_license",
                "nrega",
            }
            if minor:
                allowed_ids.add("birth_certificate")
        if (
            registration.identity_card_type
            and registration.identity_card_type not in allowed_ids
        ):
            raise ValueError(
                "The government ID type does not match nationality and age"
            )

    def validate_contact(self):
        registration = self.registration
        if registration.mobile_owner == "No phone":
            if not self.facility:
                raise ValueError("No phone registration needs a facility")
            if (
                self.patient.get("phone_number")
                or registration.whatsapp_number
                or registration.alternate_phone_number
            ):
                raise ValueError(
                    "No phone registration cannot include a main, WhatsApp or alternate mobile"
                )
            if any(
                channel != "Email" for channel in registration.communication_channels
            ):
                raise ValueError(
                    "No phone registration cannot use phone communication channels"
                )
            if (
                "Email" in registration.communication_channels
                and not registration.email
            ):
                raise ValueError("Enter an email address for email communication")
        pin = self.patient.get("pincode")
        domestic = registration.registration_type not in INTERNATIONAL_TYPES
        if (domestic or pin is not None) and (
            isinstance(pin, bool) or not re.fullmatch(r"[1-9]\d{5}", str(pin))
        ):
            raise ValueError("PIN must be 6 digits and start with 1-9")
        if domestic and (
            problem := address_problem(
                str(pin),
                registration.locality,
                registration.district,
                registration.state,
            )
        ):
            raise ValueError(problem)
        if (
            domestic
            and registration.permanent_pincode
            and (
                problem := address_problem(
                    registration.permanent_pincode,
                    registration.permanent_locality,
                    registration.permanent_district,
                    registration.permanent_state,
                )
            )
        ):
            raise ValueError(f"Permanent address: {problem}")
        for label, phone in (
            ("main mobile", self.patient.get("phone_number")),
            ("WhatsApp mobile", registration.whatsapp_number),
            ("alternate mobile", registration.alternate_phone_number),
            ("next of kin mobile", registration.next_of_kin_phone),
        ):
            if (
                phone
                and (domestic or str(phone).startswith("+91"))
                and not re.fullmatch(r"\+91[6-9]\d{9}", str(phone))
            ):
                raise ValueError(f"The {label} must have 10 Indian digits starting 6-9")

    def validate_policy_holder(self, age, minor):
        registration = self.registration
        staff = registration.payer_type == "Staff"
        dependant = staff and registration.payer_name == "POH staff — dependant"
        if minor and staff and not dependant:
            raise ValueError("A minor can only be a staff dependant, never staff self")
        needs_holder = (
            registration.payer_type in ("Corporate", "Insurance/TPA", "Govt scheme")
            or dependant
        )
        relation = registration.policy_holder_relationship
        if not needs_holder:
            if relation and not (staff and relation == "Self"):
                raise ValueError(
                    "This payer does not need a policy-holder relationship"
                )
            return
        allowed = {"Child"} if minor else {"Spouse", "Dependant parent"}
        if not minor:
            if not dependant:
                allowed.add("Self")
            if age < 25:  # noqa: PLR2004
                allowed.add("Child")
        if relation not in allowed:
            raise ValueError(
                "The policy-holder relationship does not match age and payer"
            )


class AppointmentBookSpec(BaseModel):
    slot: UUID4
    patient: UUID4
    note: str = ""
    tags: list[UUID4] = []
    booking_channel: BookingChannels
    consultation_type: ConsultationTypes = ConsultationTypes.in_person
    # Left out: follow-up if the patient is inside the schedule's revisit window.
    visit_purpose: VisitPurposes | None = None
    # "I don't know whom to meet" (D50). Red flags are refused, never stored.
    guidance_area: str = ""
    guidance_since: str = ""
    red_flags: list[str] = []
    chief_complaint: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_guidance(self):
        one_of(GUIDANCE_AREAS)(self.guidance_area)
        one_of(GUIDANCE_SINCE)(self.guidance_since)
        for flag in self.red_flags:
            one_of(GUIDANCE_RED_FLAGS, blank=False)(flag)
        if self.red_flags:
            raise ValueError(
                "Red flag: send the patient to the emergency desk, not an appointment"
            )
        self.chief_complaint = self.chief_complaint.strip()
        return self


class WalkInReserveWriteSpec(BaseModel):
    availability: UUID4
    # 4 holds back one seat in four. null removes the reserve.
    walk_in_every: int | None = Field(default=None, ge=2, le=20)


class DocumentUploadSpec(BaseModel):
    category: DocumentCategories
    side: DocumentSides
    capture_method: CaptureMethods
    source: str = Field(default="", max_length=100)
    document_date: datetime.date | None = None
    # Attaches to an existing patient; left out, the document is staged for a
    # registration.
    patient: UUID4 | None = None

    @model_validator(mode="after")
    def validate_date(self):
        self.source = self.source.strip()
        if self.document_date and self.document_date > timezone.localdate():
            raise ValueError("The document date cannot be in the future")
        return self


class StatutoryFlagSpec(BaseModel):
    encounter: UUID4
    category: StatutoryCategories


class BreakGlassSpec(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


class DeskContactSpec(BaseModel):
    phone_number: PhoneNumber = Field(max_length=14)
    mobile_owner: str

    @model_validator(mode="after")
    def validate_owner(self):
        one_of([owner for owner in MOBILE_OWNERS if owner != "No phone"], blank=False)(
            self.mobile_owner
        )
        return self
