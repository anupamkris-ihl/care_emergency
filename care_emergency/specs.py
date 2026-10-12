import datetime
import re

from care.emr.resources.base import PhoneNumber
from django.utils import timezone
from pydantic import UUID4, BaseModel, Field, field_validator, model_validator

from care_emergency.constants import (
    ABHA_STATUSES,
    ALLERGY_SEVERITIES,
    ALLERGY_STATUSES,
    ASSISTANCE_NEEDS,
    COMMUNICATION_CHANNELS,
    CONSENT_GIVERS,
    CONSENT_METHODS,
    FRRO_C_FORM,
    GUARDIAN_RELATIONSHIPS,
    IDENTITY_CARD_TYPES,
    INTERNATIONAL_TYPES,
    MARITAL_STATUSES,
    MLC_NATURES,
    MOBILE_OWNERS,
    NATIONALITY_CATEGORIES,
    PAYER_TYPES,
    PREGNANCY_STATUSES,
    PROMOTION_PURPOSES,
    REGISTRATION_TYPES,
    TITLES,
    UNIT_CODE_PATTERN,
    VISA_TYPES,
    BookingChannels,
    ConsultationTypes,
    TriageChoices,
    VisitPurposes,
)


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
        # Care raises a 500 for age=0 without a date of birth
        # (PatientCreateSpec.perform_extra_deserialization), so require an
        # estimate of at least 1 year.
        age = self.patient.get("age")
        if self.patient.get("date_of_birth"):
            return self
        if not isinstance(age, int) or isinstance(age, bool) or not 1 <= age <= 130:  # noqa: PLR2004
            raise ValueError("Enter an estimated age between 1 and 130")
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
    "guardian_relationship": (GUARDIAN_RELATIONSHIPS, True),
    "mobile_owner": (MOBILE_OWNERS, False),
    "nationality_category": (NATIONALITY_CATEGORIES, False),
    "identity_card_type": (IDENTITY_CARD_TYPES, True),
    "abha_status": (ABHA_STATUSES, True),
    "visa_type": (VISA_TYPES, True),
    "frro_c_form": (FRRO_C_FORM, True),
    "allergy_status": (ALLERGY_STATUSES, False),
    "pregnancy_status": (PREGNANCY_STATUSES, True),
    "payer_type": (PAYER_TYPES, False),
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
    alternate_phone_number: PhoneNumber | None = Field(default=None, max_length=16)
    email: str = Field(default="", max_length=254)
    house: str = Field(default="", max_length=100)
    street: str = Field(default="", max_length=150)
    locality: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
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
        if self.promotion_purposes and not self.promotions_consent:
            raise ValueError("Promotion purposes need promotional consent")
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


class DeskRegisterSpec(BaseModel):
    facility: UUID4 | None = None
    # Passed through to Care's PatientCreateSpec.
    patient: dict
    registration: RegistrationSpec
    # Issues a PRE- reference instead of a UHID; the UHID follows on arrival.
    pre_registration: bool = False

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
            age = (
                today.year
                - born.year
                - ((today.month, today.day) < (born.month, born.day))
            )
        if not isinstance(age, int) or isinstance(age, bool):
            raise ValueError("Enter the date of birth or the age")
        minor = age < 18  # noqa: PLR2004
        if minor and not dob:
            raise ValueError("Patients under 18 need an exact date of birth")
        if minor and not (
            registration.guardian_name
            and registration.guardian_relationship
            and registration.next_of_kin_name
            and registration.next_of_kin_phone
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
        return self


class AppointmentBookSpec(BaseModel):
    slot: UUID4
    patient: UUID4
    note: str = ""
    tags: list[UUID4] = []
    booking_channel: BookingChannels
    consultation_type: ConsultationTypes = ConsultationTypes.in_person
    # Left out: follow-up if the patient is inside the schedule's revisit window.
    visit_purpose: VisitPurposes | None = None


class WalkInReserveWriteSpec(BaseModel):
    availability: UUID4
    # 4 holds back one seat in four. null removes the reserve.
    walk_in_every: int | None = Field(default=None, ge=2, le=20)
