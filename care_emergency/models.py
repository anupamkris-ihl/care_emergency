from care.emr.models.base import EMRBaseModel
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from care_emergency.constants import (
    BookingChannels,
    CaptureMethods,
    ConsultationTypes,
    DocumentCategories,
    DocumentSides,
    StatutoryCategories,
    TriageChoices,
    VisitPurposes,
)


class EmergencyDesk(EMRBaseModel):
    """The facility-owned number used as phone_number for emergency patients."""

    facility = models.OneToOneField(
        "facility.Facility", on_delete=models.PROTECT, related_name="emergency_desk"
    )
    phone_number = models.CharField(max_length=14)


class EmergencyEncounter(EMRBaseModel):
    encounter = models.OneToOneField(
        "emr.Encounter", on_delete=models.PROTECT, related_name="emergency"
    )
    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="emergency_encounters"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    triage = models.CharField(max_length=10, choices=TriageChoices.choices)
    # Nullable so an update can set it back to false (Care drops default values).
    is_medico_legal = models.BooleanField(null=True)
    mlc_nature = models.CharField(max_length=50, blank=True, default="")
    police_station = models.CharField(max_length=200, blank=True, default="")
    mlc_number = models.CharField(max_length=30, null=True, blank=True)
    distinguishing_marks = models.TextField(blank=True, default="")
    brought_by_name = models.CharField(max_length=200, blank=True, default="")
    brought_by_phone = models.CharField(max_length=14, blank=True, default="")
    brought_by_relationship = models.CharField(max_length=50, blank=True, default="")
    is_unidentified = models.BooleanField(default=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "mlc_number"],
                condition=Q(mlc_number__isnull=False),
                name="care_emergency_unique_mlc_number",
            )
        ]


class MlcSequence(models.Model):
    """One counter row per facility per year, incremented under a row lock."""

    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    year = models.PositiveIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "year"], name="care_emergency_unique_mlc_sequence"
            )
        ]

    def __str__(self):
        return f"{self.facility_id}/{self.year}: {self.last_value}"


class PatientRegistration(EMRBaseModel):
    """Everything the front desk records about a patient beyond Care's core
    patient fields, one column per field. Core fields (name, gender, phone,
    date of birth, address, PIN, blood group, identifiers) stay on Patient."""

    patient = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="desk_registration"
    )
    facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    registration_type = models.CharField(max_length=10)

    # Identity
    title = models.CharField(max_length=10)
    first_name = models.CharField(max_length=40)
    middle_name = models.CharField(max_length=40, blank=True, default="")
    last_name = models.CharField(max_length=40, blank=True, default="")
    marital_status = models.CharField(max_length=20, blank=True, default="")
    father_name = models.CharField(max_length=100, blank=True, default="")
    mother_name = models.CharField(max_length=100, blank=True, default="")
    guardian_name = models.CharField(max_length=40, blank=True, default="")
    guardian_relationship = models.CharField(max_length=10, blank=True, default="")
    occupation = models.CharField(max_length=50, blank=True, default="")
    religion = models.CharField(max_length=30, blank=True, default="")
    preferred_language = models.CharField(max_length=30)

    # Contact
    mobile_owner = models.CharField(max_length=20)
    whatsapp_number = models.CharField(max_length=16, blank=True, default="")
    alternate_phone_number = models.CharField(max_length=16, blank=True, default="")
    email = models.CharField(max_length=254, blank=True, default="")
    house = models.CharField(max_length=100, blank=True, default="")
    street = models.CharField(max_length=150, blank=True, default="")
    locality = models.CharField(max_length=100, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    next_of_kin_name = models.CharField(max_length=40, blank=True, default="")
    next_of_kin_relationship = models.CharField(max_length=30, blank=True, default="")
    next_of_kin_phone = models.CharField(max_length=16, blank=True, default="")

    # Documentation. Only the ID type and its last 4 characters are kept.
    nationality_category = models.CharField(max_length=20)
    identity_storage_consent = models.BooleanField(default=False)
    identity_card_type = models.CharField(max_length=20, blank=True, default="")
    identity_number_last4 = models.CharField(max_length=4, blank=True, default="")
    abha_status = models.CharField(max_length=10, blank=True, default="")
    abha_number = models.CharField(max_length=14, blank=True, default="")
    abha_address = models.CharField(max_length=60, blank=True, default="")

    # Travel and residency (international patients)
    nationality = models.CharField(max_length=60, blank=True, default="")
    passport_number = models.CharField(max_length=15, blank=True, default="")
    passport_expiry = models.DateField(null=True, blank=True)
    visa_type = models.CharField(max_length=30, blank=True, default="")
    visa_expiry = models.DateField(null=True, blank=True)
    frro_c_form = models.CharField(max_length=50, blank=True, default="")
    address_in_india = models.CharField(max_length=300, blank=True, default="")
    interpreter_language = models.CharField(max_length=30, blank=True, default="")

    # Safety flags; allergies are RegistrationAllergy rows.
    allergy_status = models.CharField(max_length=10)
    assistance_needs = ArrayField(
        models.CharField(max_length=30), default=list, blank=True
    )
    pregnancy_status = models.CharField(max_length=20, blank=True, default="")

    # VIP protocol (discreet handling only; no effect on price or care)
    is_vip = models.BooleanField(default=False)
    vip_note = models.CharField(max_length=200, blank=True, default="")
    vip_authorised_by = models.CharField(max_length=100, blank=True, default="")
    handle_with_care = models.BooleanField(default=False)
    vip_flagged_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Payer
    payer_type = models.CharField(max_length=20)
    payer_name = models.CharField(max_length=200, blank=True, default="")
    employee_id = models.CharField(max_length=24, blank=True, default="")
    policy_number = models.CharField(max_length=24, blank=True, default="")
    policy_valid_till = models.DateField(null=True, blank=True)
    policy_holder_relationship = models.CharField(max_length=30, blank=True, default="")
    concession = models.CharField(max_length=60, blank=True, default="")
    concession_approved_by = models.CharField(max_length=100, blank=True, default="")

    # Source and referral
    arrival_source = models.CharField(max_length=40)
    lead_source = models.CharField(max_length=60, blank=True, default="")
    camp_name = models.CharField(max_length=100, blank=True, default="")
    camp_patient_id = models.CharField(max_length=30, blank=True, default="")
    referring_provider = models.CharField(max_length=200, blank=True, default="")
    referrer_registration_number = models.CharField(
        max_length=30, blank=True, default=""
    )
    referrer_organisation = models.CharField(max_length=200, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    # Consent and communication
    consent_given_by = models.CharField(max_length=20)
    consent_method = models.CharField(max_length=20)
    consent_witness = models.CharField(max_length=40, blank=True, default="")
    consent_language = models.CharField(max_length=30)
    general_consent = models.BooleanField(default=False)
    data_processing_consent = models.BooleanField(default=False)
    tariff_consent = models.BooleanField(default=False)
    share_with_referrer_consent = models.BooleanField(default=False)
    abdm_link_consent = models.BooleanField(default=False)
    teleconsultation_consent = models.BooleanField(default=False)
    clinical_photography_consent = models.BooleanField(default=False)
    research_consent = models.BooleanField(default=False)
    communication_channels = ArrayField(
        models.CharField(max_length=20), default=list, blank=True
    )
    promotions_consent = models.BooleanField(default=False)
    promotion_purposes = ArrayField(
        models.CharField(max_length=30), default=list, blank=True
    )


class RegistrationAllergy(models.Model):
    registration = models.ForeignKey(
        PatientRegistration, on_delete=models.CASCADE, related_name="allergies"
    )
    substance = models.CharField(max_length=100)
    reaction = models.CharField(max_length=100, blank=True, default="")
    severity = models.CharField(max_length=20)

    def __str__(self):
        return self.substance


class AppointmentDetails(EMRBaseModel):
    """What the front desk records about a Care booking beyond patient, note
    and tags: the APT number, consultation type, visit purpose and channel."""

    booking = models.OneToOneField(
        "emr.TokenBooking", on_delete=models.PROTECT, related_name="desk_details"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    appointment_number = models.CharField(max_length=20, unique=True)
    consultation_type = models.CharField(
        max_length=20, choices=ConsultationTypes.choices
    )
    visit_purpose = models.CharField(max_length=20, choices=VisitPurposes.choices)
    booking_channel = models.CharField(max_length=20, choices=BookingChannels.choices)
    # Phone bookings only; sent by the send_appointment_reminders task.
    reminder_due_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # "I don't know whom to meet" (D50); the remarks print as the chief complaint.
    guidance_area = models.CharField(max_length=40, blank=True, default="")
    guidance_since = models.CharField(max_length=20, blank=True, default="")
    chief_complaint = models.CharField(max_length=500, blank=True, default="")


class AppointmentSequence(models.Model):
    """One counter row per year for APT numbers, incremented under a row lock.
    The number has no site code, so the counter is shared by all facilities."""

    year = models.PositiveIntegerField(unique=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_value}"


class WalkInReserve(EMRBaseModel):
    """Holds back one in every `walk_in_every` seats of an availability each
    day for walk-ins; scheduled bookings can't take them."""

    availability = models.OneToOneField(
        "emr.Availability", on_delete=models.CASCADE, related_name="walk_in_reserve"
    )
    walk_in_every = models.PositiveSmallIntegerField()


class FacilityUnit(EMRBaseModel):
    """The facility's unit code (LN, KB, DW), printed in its patient numbers."""

    facility = models.OneToOneField(
        "facility.Facility", on_delete=models.PROTECT, related_name="unit"
    )
    code = models.CharField(max_length=4, unique=True)


class PatientNumberSequence(models.Model):
    """One counter row per series, unit and period, incremented under a row
    lock. period is the day for daily series (UNK) and "" otherwise. Counters
    only go up, so a number is never issued twice."""

    series = models.CharField(max_length=3)
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    period = models.CharField(max_length=8, blank=True, default="")
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["series", "facility", "period"],
                name="care_emergency_unique_patient_number_sequence",
            )
        ]

    def __str__(self):
        return f"{self.series}/{self.facility_id}/{self.period}: {self.last_value}"


class PatientDocument(EMRBaseModel):
    """A document captured at the desk (D37, D43). The file is a Care
    FileUpload of the patient. Until the registration that uses it is saved,
    patient is null and the file belongs to nobody (staged)."""

    file = models.OneToOneField(
        "emr.FileUpload", on_delete=models.PROTECT, related_name="desk_document"
    )
    patient = models.ForeignKey(
        "emr.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="desk_documents",
    )
    category = models.CharField(max_length=30, choices=DocumentCategories.choices)
    side = models.CharField(max_length=10, choices=DocumentSides.choices)
    capture_method = models.CharField(max_length=20, choices=CaptureMethods.choices)


class ArtCouple(EMRBaseModel):
    """An IVF / ART couple registered together (D45). The woman is the
    registered patient; the partner gets their own Care patient. A single
    woman has no partner and needs no marriage certificate."""

    woman = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="art_couple"
    )
    partner = models.OneToOneField(
        "emr.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="art_partner_of",
    )
    facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    single_woman = models.BooleanField(default=False)
    art_consent = models.BooleanField(default=False)
    # The partner's ID summary, kept like the patient's: type and last 4 only.
    partner_identity_card_type = models.CharField(max_length=20, blank=True, default="")
    partner_identity_number_last4 = models.CharField(
        max_length=4, blank=True, default=""
    )
    partner_abha_number = models.CharField(max_length=14, blank=True, default="")
    partner_abha_address = models.CharField(max_length=60, blank=True, default="")


class RestrictedPatient(models.Model):
    """A patient under statutory confidentiality (D30). Created with the first
    flag; from then on the patient's identity is masked behind the alias for
    everyone except the flagging clinicians and break-glass access."""

    patient = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="restriction"
    )
    alias = models.CharField(max_length=9, unique=True)
    created_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.alias


class StatutoryFlag(EMRBaseModel):
    """One statutory flag, raised by a clinician in an encounter. The category
    is never shown to the front desk."""

    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="statutory_flags"
    )
    encounter = models.ForeignKey(
        "emr.Encounter", on_delete=models.PROTECT, related_name="+"
    )
    category = models.CharField(max_length=20, choices=StatutoryCategories.choices)


class BreakGlassAccess(models.Model):
    """An audited, time-limited unmasking of a restricted patient for one
    user. Rows are never edited or deleted."""

    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=500)
    created_date = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"{self.user_id} → {self.patient_id} until {self.expires_at}"
