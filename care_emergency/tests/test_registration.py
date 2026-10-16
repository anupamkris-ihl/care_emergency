import datetime
from unittest import mock

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import FileUpload, Patient
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.models import (
    ArtCouple,
    FacilityUnit,
    EmergencyDesk,
    PatientDocument,
    PatientRegistration,
    PostOffice,
)

PHONE = "+919812345678"
INDIAN_ALTERNATE = "+919898989898"
# What every international registration adds: an Indian alternate mobile and
# the international tariff consent.
INTERNATIONAL = {
    "alternate_phone_number": INDIAN_ALTERNATE,
    "international_tariff_consent": True,
}


def stage_document(user, category, side="single"):
    """A staged desk document, as documents/ leaves it before registration."""
    file = FileUpload.objects.create(
        name=category,
        internal_name="scan.jpg",
        associating_id="",
        file_type="patient",
        file_category="unspecified",
        upload_completed=True,
        meta={"mime_type": "image/jpeg"},
        created_by=user,
    )
    document = PatientDocument.objects.create(
        file=file,
        category=category,
        side=side,
        capture_method="photographed",
        created_by=user,
    )
    return str(document.external_id)


def in_years(years):
    today = timezone.localdate()
    return today.replace(year=today.year + years).isoformat()


def registration_details(**kwargs):
    data = {
        "registration_type": "DOM",
        "title": "Ms",
        "first_name": "Neha",
        "last_name": "Sharma",
        "marital_status": "single",
        "preferred_language": "Hindi",
        "mobile_owner": "Self",
        "whatsapp_number": PHONE,
        "house": "C-22",
        "street": "Ring Road",
        "locality": "Lajpat Nagar III",
        "city": "New Delhi",
        "district": "South East Delhi",
        "state": "Delhi",
        "nationality_category": "Indian resident",
        "identity_storage_consent": True,
        "identity_card_type": "aadhaar",
        "identity_number_last4": "7741",
        "abha_status": "has",
        "abha_address": "neha.sharma@abdm",
        "allergy_status": "has",
        "allergies": [
            {"substance": "Penicillin", "reaction": "Hives", "severity": "moderate"},
            {
                "substance": "Latex",
                "reaction": "Anaphylaxis",
                "severity": "life_threatening",
            },
        ],
        "assistance_needs": ["Wheelchair"],
        "pregnancy_status": "not_pregnant",
        "payer_type": "Self",
        "concession": "None",
        "arrival_source": "Walk-in",
        "lead_source": "Word of mouth",
        "remarks": "Prefers morning slots",
        "consent_given_by": "The patient",
        "consent_method": "Signature",
        "consent_language": "Hindi",
        "general_consent": True,
        "data_processing_consent": True,
        "tariff_consent": True,
        "research_consent": True,
        "communication_channels": ["WhatsApp", "SMS"],
        "promotions_consent": True,
        "promotion_purposes": ["Health tips", "Camps"],
    }
    data.update(kwargs)
    return data


class DeskRegistrationTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()

    def body(self, patient=None, documents=(), **registration):
        return {
            "facility": str(self.facility.external_id),
            "documents": list(documents),
            "patient": {
                "name": "Neha Sharma",
                "gender": "female",
                "phone_number": PHONE,
                "date_of_birth": "1999-04-12",
                "address": "C-22, Ring Road, Lajpat Nagar III, Delhi",
                "pincode": 110024,
                **(patient or {}),
            },
            "registration": registration_details(**registration),
        }

    def register(self, body):
        # Care releases its create locks on commit; run those callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("desk-registration"), body, format="json")

    def assert_rejected(self, body, message):
        response = self.register(body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(message, str(response.data))
        self.assertFalse(Patient.objects.exists())
        self.assertFalse(PatientRegistration.objects.exists())

    def test_register_creates_patient_and_registration(self):
        response = self.register(self.body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.street, "Ring Road")
        self.assertEqual(record.city, "New Delhi")
        self.assertEqual(record.promotion_purposes, ["Health tips", "Camps"])
        self.assertEqual(record.abha_address, "neha.sharma@abdm")
        self.assertEqual(record.arrival_source, "Walk-in")
        self.assertEqual(record.lead_source, "Word of mouth")
        self.assertEqual(record.assistance_needs, ["Wheelchair"])
        self.assertEqual(record.alternate_phone_number, "")
        self.assertTrue(record.research_consent)
        self.assertEqual(record.allergies.count(), 2)
        self.assertIsNone(record.vip_flagged_by)

        detail = self.client.get(
            reverse("desk-registration-detail", args=[patient.external_id])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["first_name"], "Neha")
        self.assertEqual(
            [a["severity"] for a in detail.data["allergies"]],
            ["moderate", "life_threatening"],
        )

    def test_international_patient_keeps_interpreter_and_c_form(self):
        response = self.register(
            self.body(
                patient={"phone_number": "+447911123456", "pincode": None},
                documents=[
                    stage_document(self.user, "passport"),
                    stage_document(self.user, "visa"),
                ],
                registration_type="INT",
                nationality_category="Foreign",
                nationality="United Kingdom",
                passport_number="GBR5521104",
                passport_expiry=in_years(2),
                visa_type="e-Medical",
                visa_expiry=in_years(1),
                frro_c_form="Auto-file within 24 h",
                address_in_india="Hotel Pacific, Saket",
                interpreter_language="Arabic",
                identity_card_type="passport",
                identity_number_last4="1104",
                abha_status="",
                abha_address="",
                whatsapp_number=None,
                payer_type="International",
                is_vip=True,
                vip_note="Embassy guest",
                vip_authorised_by="Vikram Shah — Centre Manager",
                handle_with_care=True,
                **INTERNATIONAL,
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get()
        self.assertTrue(record.international_tariff_consent)
        self.assertEqual(record.interpreter_language, "Arabic")
        self.assertEqual(record.frro_c_form, "Auto-file within 24 h")
        self.assertTrue(record.is_vip)
        self.assertEqual(record.vip_flagged_by, self.user)
        documents = PatientDocument.objects.filter(patient=record.patient)
        self.assertEqual(documents.count(), 2)
        self.assertEqual(
            {x.file.associating_id for x in documents},
            {str(record.patient.external_id)},
        )

    def test_nri_and_oci_need_their_passport_not_a_passport_id(self):
        nri = {
            "registration_type": "NRI",
            "nationality_category": "NRI",
            "nationality": "India",
            "passport_number": "Z1234567",
            "address_in_india": "C-22, Ring Road, Lajpat Nagar III",
            "permanent_house": "14 Baker Street",
            "permanent_city": "London",
            "permanent_country": "United Kingdom",
            "permanent_pincode": "NW1 6XE",
            **INTERNATIONAL,
        }
        oci = {
            "registration_type": "INT",
            "nationality_category": "OCI-PIO",
            "nationality": "USA",
            "passport_number": "A12345678",
            "identity_card_type": "oci_card",
            "identity_number_last4": "4410",
            "address_in_india": "Hotel Pacific, Saket",
            "whatsapp_number": None,
            "payer_type": "International",
            **INTERNATIONAL,
        }
        self.assert_rejected(
            self.body(**{**oci, "passport_number": ""}),
            "Enter the passport number",
        )
        # The government ID stays the Aadhaar; the passport has its own field.
        response = self.register(self.body(**nri))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get()
        self.assertEqual(record.identity_card_type, "aadhaar")
        self.assertEqual(record.passport_number, "Z1234567")
        self.assertEqual(record.permanent_country, "United Kingdom")
        self.assertEqual(record.permanent_pincode, "NW1 6XE")
        PatientCreateLock().release()
        response = self.register(
            self.body(
                patient={
                    "name": "Maya Iyer",
                    "phone_number": "+14155550134",
                    "pincode": None,
                },
                **oci,
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def foreign_body(self, patient=None, documents=None, **registration):
        """A foreign national as the International desk screen sends one: the
        passport comes from the government ID, with no expiry or C-Form."""
        if documents is None:
            documents = [
                stage_document(self.user, "passport"),
                stage_document(self.user, "visa"),
            ]
        return self.body(
            patient={
                "phone_number": "+447911123456",
                "pincode": None,
                **(patient or {}),
            },
            documents=documents,
            **{
                "registration_type": "INT",
                "nationality_category": "Foreign",
                "nationality": "United Kingdom",
                "identity_card_type": "passport",
                "identity_number_last4": "1104",
                "passport_number": "GBR5521104",
                "visa_type": "Medical (MED)",
                "visa_expiry": in_years(1),
                "address_in_india": "Hotel Pacific, Saket",
                "whatsapp_number": "+447911123456",
                "abha_status": "",
                "abha_address": "",
                **INTERNATIONAL,
                **registration,
            },
        )

    def test_international_screen_rules(self):
        cases = [
            ({"international_tariff_consent": False}, "international tariff"),
            ({"alternate_phone_number": None}, "Indian alternate mobile"),
            ({"alternate_phone_number": "+447911123457"}, "Indian alternate mobile"),
            ({"nationality": ""}, "country of origin"),
            ({"visa_type": ""}, "visa status"),
            ({"visa_expiry": None}, "visa validity date"),
            ({"visa_type": "Not a visa"}, "is not one of"),
            ({"identity_number_last4": "9999"}, "does not match the ID number"),
            (
                {
                    "nationality_category": "Refugee",
                    "identity_card_type": "",
                    "identity_number_last4": "",
                    "passport_number": "",
                },
                "UNHCR card or the passport",
            ),
        ]
        for registration, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(self.foreign_body(**registration), message)

        response = self.register(self.foreign_body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get()
        self.assertEqual(record.passport_number, "GBR5521104")
        self.assertIsNone(record.passport_expiry)
        self.assertEqual(record.frro_c_form, "")
        self.assertEqual(record.alternate_phone_number, INDIAN_ALTERNATE)

    def test_visa_free_and_refugee_need_no_visa_or_passport(self):
        response = self.register(
            self.foreign_body(
                documents=[stage_document(self.user, "passport")],
                visa_type="OCI — visa-free",
                visa_expiry=None,
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        PatientCreateLock().release()
        response = self.register(
            self.foreign_body(
                patient={"name": "Ahmad Karimi", "phone_number": "+93701234567"},
                documents=[],
                nationality_category="Refugee",
                nationality="Afghanistan",
                identity_card_type="unhcr_card",
                identity_number_last4="5521",
                passport_number="",
                visa_type="",
                visa_expiry=None,
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_next_of_kin_title_and_staff_payer_names(self):
        for registration, message in (
            ({"next_of_kin_title": "Mrs"}, "next of kin's name with their title"),
            (
                {"next_of_kin_title": "Sir", "next_of_kin_name": "Ravi Sharma"},
                "'Sir' is not one of",
            ),
            (
                {
                    "registration_type": "STAFF",
                    "payer_type": "Staff",
                    "payer_name": "POH staff",
                    "employee_id": "POH-1182",
                },
                "'POH staff' is not one of",
            ),
            ({"permanent_pincode": "012345"}, "permanent PIN must be 6 digits"),
            (
                {"permanent_country": "Nepal", "permanent_pincode": "-"},
                "permanent address's postal code",
            ),
        ):
            with self.subTest(registration=registration):
                self.assert_rejected(self.body(**registration), message)
        dependant = {
            "registration_type": "STAFF",
            "payer_type": "Staff",
            "payer_name": "POH staff — dependant",
            "staff_category": "Doctor",
            "employee_id": "POH-1182",
            "policy_holder_relationship": "Spouse",
            "next_of_kin_title": "Dr",
            "next_of_kin_name": "Ravi Sharma",
            "next_of_kin_relationship": "Spouse",
        }
        front = stage_document(self.user, "payer_card", "front")
        back = stage_document(self.user, "payer_card", "back")
        response = self.register(self.body(documents=[front, back], **dependant))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["registration"]["next_of_kin_title"], "Dr")
        self.assertEqual(
            PatientRegistration.objects.get().payer_name, "POH staff — dependant"
        )

    def test_any_eighth_schedule_language_is_accepted(self):
        # v12.1 D11 / M06: languages and religions come from the masters.
        response = self.register(
            self.body(preferred_language="Santali", consent_language="Santali")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            PatientRegistration.objects.get().preferred_language, "Santali"
        )

    def test_language_outside_the_master_is_rejected(self):
        self.assert_rejected(self.body(preferred_language="Klingon"), "'Klingon'")

    def test_consent_language_outside_the_master_is_rejected(self):
        self.assert_rejected(self.body(consent_language="Klingon"), "'Klingon'")

    def test_religion_outside_the_master_is_rejected(self):
        self.assert_rejected(self.body(religion="Jedi"), "'Jedi'")

    def test_non_self_payer_needs_both_sides_of_the_card(self):
        corporate = {
            "registration_type": "CORP",
            "payer_type": "Corporate",
            "payer_name": "Infosys",
            "employee_id": "INF-2231",
            "policy_holder_relationship": "Self",
        }
        self.assert_rejected(self.body(**corporate), "the payer card (front and back)")
        front = stage_document(self.user, "payer_card", "front")
        self.assert_rejected(
            self.body(documents=[front], **corporate), "the payer card"
        )
        back = stage_document(self.user, "payer_card", "back")
        response = self.register(self.body(documents=[front, back], **corporate))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # A one-page sponsor letter is enough on its own.
        PatientCreateLock().release()
        letter = stage_document(self.user, "payer_card", "single")
        response = self.register(
            self.body(
                patient={"name": "Asha Rao", "phone_number": "+919812345600"},
                documents=[letter],
                **corporate,
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_whatsapp_owner_staff_category_and_permanent_address(self):
        staff = {
            "registration_type": "STAFF",
            "payer_type": "Staff",
            "payer_name": "POH staff — self",
            "staff_category": "Nurse",
            "employee_id": "POH-1182",
            "whatsapp_owner": "Spouse",
            "mobile_owner": "Father",
            "communication_channels": ["IVR"],
            "permanent_house": "12",
            "permanent_locality": "Fort",
            "permanent_city": "Mumbai",
            "permanent_district": "Mumbai",
            "permanent_state": "Maharashtra",
            "permanent_pincode": "400001",
        }
        PostOffice.objects.create(
            pincode="400001",
            name="Fort",
            district="Mumbai",
            state="Maharashtra",
            loaded_at=timezone.now(),
        )
        for registration, message in (
            ({"staff_category": "Pilot"}, "'Pilot' is not one of"),
            ({"whatsapp_owner": "Cousin"}, "'Cousin' is not one of"),
            ({"whatsapp_number": None}, "Enter the WhatsApp number"),
            (
                {"permanent_district": "Pune"},
                "Permanent address: The district and state do not match the PIN",
            ),
            ({"permanent_pincode": "012345"}, "permanent_pincode"),
            ({"do_not_contact": True}, "Do not contact cannot have"),
        ):
            with self.subTest(registration=registration):
                self.assert_rejected(self.body(**{**staff, **registration}), message)
        self.assert_rejected(
            self.body(staff_category="Doctor"), "only for staff payers"
        )

        front = stage_document(self.user, "payer_card", "front")
        back = stage_document(self.user, "payer_card", "back")
        response = self.register(self.body(documents=[front, back], **staff))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get()
        self.assertEqual(record.staff_category, "Nurse")
        self.assertEqual(record.whatsapp_owner, "Spouse")
        self.assertEqual(record.mobile_owner, "Father")
        self.assertEqual(record.communication_channels, ["IVR"])
        self.assertEqual(record.permanent_pincode, "400001")
        self.assertEqual(response.data["registration"]["permanent_city"], "Mumbai")

    def test_do_not_contact_is_kept(self):
        body = self.body(
            do_not_contact=True,
            communication_channels=[],
            promotions_consent=False,
            promotion_purposes=[],
        )
        response = self.register(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(PatientRegistration.objects.get().do_not_contact)
        self.assertTrue(response.data["registration"]["do_not_contact"])

    def test_gender_other_is_accepted(self):
        response = self.register(
            self.body(patient={"gender": "other"}, title="Mx", pregnancy_status="")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Patient.objects.get().gender, "other")

    def test_documents_must_be_the_users_own_and_unused(self):
        other = self.create_super_user()
        foreign = stage_document(other, "passport")
        self.assert_rejected(self.body(documents=[foreign]), "upload it again")

    def couple_body(self, partner=None, couple=None, **kwargs):
        partner_body = {
            "first_name": "Rohit",
            "last_name": "Sharma",
            "date_of_birth": "1988-02-10",
            "phone_number": "+919812345699",
            "identity_card_type": "pan",
            "identity_number_last4": "234F",
            "documents": [stage_document(self.user, "government_id", "front")],
            **(partner or {}),
        }
        body = self.body(
            documents=kwargs.pop(
                "documents",
                [
                    stage_document(self.user, "government_id", "front"),
                    stage_document(self.user, "marriage_certificate"),
                ],
            ),
            registration_type="CPL",
            **kwargs,
        )
        body["couple"] = {
            "partner": partner_body,
            "art_consent": True,
            **(couple or {}),
        }
        return body

    def test_ivf_couple_registers_both_partners(self):
        response = self.register(self.couple_body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        couple = ArtCouple.objects.get()
        woman = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.assertEqual(couple.woman, woman)
        self.assertEqual(couple.partner.name, "Rohit Sharma")
        self.assertEqual(couple.partner.gender, "male")
        self.assertEqual(couple.partner.address, woman.address)
        self.assertEqual(couple.partner_identity_number_last4, "234F")
        self.assertTrue(couple.art_consent)
        self.assertEqual(
            PatientDocument.objects.get(patient=couple.partner).category,
            "government_id",
        )
        self.assertEqual(PatientDocument.objects.filter(patient=woman).count(), 2)
        self.assertTrue(couple.partner.instance_identifiers)
        detail = self.client.get(
            reverse("desk-registration-detail", args=[woman.external_id])
        )
        self.assertEqual(
            detail.data["couple"]["partner"], str(couple.partner.external_id)
        )

    def test_single_woman_needs_no_partner_or_certificate(self):
        body = self.couple_body(
            documents=[stage_document(self.user, "government_id")],
            couple={"single_woman": True, "partner": None},
        )
        response = self.register(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(ArtCouple.objects.get().partner)

    def test_ivf_couple_rules(self):
        cases = [
            (
                self.couple_body(patient={"date_of_birth": "2008-01-01"}),
                "21-50 years old",
            ),
            (
                self.couple_body(partner={"date_of_birth": "1960-01-01"}),
                "21-55 years old",
            ),
            (self.couple_body(patient={"gender": "male"}), "the woman"),
            (self.couple_body(couple={"art_consent": False}), "ART consent"),
            (
                self.couple_body(couple={"single_woman": True}),
                "single woman",
            ),
            (
                self.couple_body(
                    documents=[stage_document(self.user, "government_id")]
                ),
                "the marriage certificate",
            ),
            (
                self.couple_body(partner={"documents": []}),
                "the partner's government ID",
            ),
            (
                self.couple_body(partner={"identity_number_last4": "2345"}),
                "partner's ID number's format",
            ),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(body, message)
        not_couple = self.body()
        not_couple["couple"] = self.couple_body()["couple"]
        self.assert_rejected(not_couple, "IVF couple type")

    def test_registration_rules(self):
        cases = [
            ({"general_consent": False}, "consents are all required"),
            ({"allergies": []}, "List the allergies"),
            ({"allergy_status": "nka"}, "List the allergies"),
            ({"identity_storage_consent": False}, "Consent is required"),
            ({"abha_address": "neha@gmail.com"}, "name@abdm"),
            ({"title": "Sir"}, "is not one of"),
            ({"first_name": "N"}, "first name"),
            ({"registration_type": "INT"}, "does not match"),
            ({"payer_type": "Corporate"}, "Choose the payer"),
            (
                {
                    "payer_type": "Govt scheme",
                    "payer_name": "CGHS",
                    "policy_number": "CG1",
                },
                "occupation",
            ),
            ({"is_vip": True}, "VIP flag needs"),
            ({"consent_method": "Thumb impression"}, "witness"),
            ({"policy_valid_till": "2000-01-01"}, "has expired"),
            ({"alternate_phone_number": PHONE}, "must differ"),
            ({"promotions_consent": False}, "need promotional consent"),
            ({"promotion_purposes": ["Lottery"]}, "is not one of"),
        ]
        for registration, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(self.body(**registration), message)

    def test_minor_needs_dob_guardian_and_next_of_kin(self):
        child = {"date_of_birth": f"{timezone.localdate().year - 10}-01-01"}
        adult_fields = {"marital_status": "", "pregnancy_status": ""}
        self.assert_rejected(
            self.body(patient=child, **adult_fields), "guardian and a next of kin"
        )
        self.assert_rejected(
            self.body(patient={"date_of_birth": None, "age": 10}, **adult_fields),
            "exact date of birth",
        )
        response = self.register(
            self.body(
                patient=child,
                **adult_fields,
                title="Ms",
                mobile_owner="Parent",
                guardian_name="Rakesh Sharma",
                guardian_relationship="D/O",
                next_of_kin_name="Rakesh Sharma",
                next_of_kin_relationship="Father",
                next_of_kin_phone="+919811640021",
                consent_given_by="Parent",
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def child_body(self, **registration):
        return self.body(
            patient={"date_of_birth": f"{timezone.localdate().year - 10}-01-01"},
            **{
                "marital_status": "",
                "pregnancy_status": "",
                "mobile_owner": "Parent",
                "guardian_name": "Rakesh Sharma",
                "guardian_relationship": "D/O",
                "next_of_kin_name": "Rakesh Sharma",
                "next_of_kin_relationship": "Father",
                "next_of_kin_phone": "+919811640021",
                "consent_given_by": "Parent",
                **registration,
            },
        )

    def test_address_must_match_a_listed_pin(self):
        PostOffice.objects.create(
            pincode="110024",
            name="Lajpat Nagar III",
            district="South East Delhi",
            state="Delhi",
            loaded_at=timezone.now(),
        )
        for registration, message in (
            ({"locality": ""}, "Choose the PIN's post office"),
            ({"locality": "Connaught Place"}, "not under this PIN"),
            ({"district": "New Delhi"}, "do not match the PIN"),
        ):
            with self.subTest(registration=registration):
                self.assert_rejected(self.body(**registration), message)
        body = self.body(locality=" lajpat nagar  iii", state="DELHI")
        self.assertEqual(self.register(body).status_code, status.HTTP_200_OK)

    def test_unlisted_pin_is_typed_by_hand(self):
        PostOffice.objects.create(
            pincode="400001",
            name="Fort",
            district="Mumbai",
            state="Maharashtra",
            loaded_at=timezone.now(),
        )
        body = self.body(locality="", district="Anywhere")
        self.assertEqual(self.register(body).status_code, status.HTTP_200_OK)

    def test_pin_and_mobile_rules_cannot_be_bypassed(self):
        for pin in (None, 0, 11002, 1100245, "010024", True, "abc123"):
            with self.subTest(pin=pin):
                self.assert_rejected(self.body(patient={"pincode": pin}), "PIN must")
        for phone in ("+915123456789", "+447911123456", "+91981234567"):
            with self.subTest(phone=phone):
                self.assert_rejected(
                    self.body(patient={"phone_number": phone}), "main mobile"
                )
        for field in ("whatsapp_number", "alternate_phone_number", "next_of_kin_phone"):
            with self.subTest(field=field):
                self.assert_rejected(
                    self.body(**{field: "+915123456789"}), "starting 6-9"
                )

    def test_demographic_rules_cannot_be_bypassed(self):
        for registration, message in (
            ({"title": "Mr"}, "title does not match"),
            ({"guardian_relationship": "S/O"}, "guardian relation"),
            ({"house": " "}, "house, flat or building"),
            ({"identity_card_type": "oci_card"}, "nationality and age"),
            ({"identity_card_type": "birth_certificate"}, "nationality and age"),
        ):
            with self.subTest(registration=registration):
                self.assert_rejected(self.body(**registration), message)
        for registration, message in (
            ({"title": "Mrs"}, "title does not match"),
            ({"occupation": "Business"}, "minor's occupation"),
            ({"mobile_owner": "Self"}, "minor's mobile"),
            ({"consent_given_by": "The patient"}, "minor's consent"),
            ({"next_of_kin_relationship": ""}, "kin's relation"),
        ):
            with self.subTest(registration=registration):
                self.assert_rejected(self.child_body(**registration), message)

    def test_id_number_formats_cannot_be_bypassed(self):
        for card_type, bad in (
            ("aadhaar", "774A"),
            ("pan", "2345"),
            ("voter_id", "ABC7"),
            ("driving_license", "DL01"),
            ("passport", "456A"),
        ):
            with self.subTest(card_type=card_type):
                self.assert_rejected(
                    self.body(identity_card_type=card_type, identity_number_last4=bad),
                    "ID number's format",
                )
        nri = {
            "registration_type": "NRI",
            "nationality_category": "NRI",
            "nationality": "UAE",
            "address_in_india": "Hotel Pacific, Saket",
            **INTERNATIONAL,
        }
        for passport in ("GBR5521104", "12345678", "AB123456"):
            with self.subTest(passport=passport):
                self.assert_rejected(
                    self.body(**nri, passport_number=passport), "Indian passport"
                )
        response = self.register(self.body(**nri, passport_number="Z1234567"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_policy_holder_rules_cannot_be_bypassed(self):
        corporate = {
            "registration_type": "CORP",
            "payer_type": "Corporate",
            "payer_name": "Infosys",
            "employee_id": "INF-2231",
        }
        for relation in ("", "Friend", "Child"):
            with self.subTest(relation=relation):
                self.assert_rejected(
                    self.body(**corporate, policy_holder_relationship=relation),
                    "policy-holder relationship",
                )
        self.assert_rejected(
            self.child_body(**corporate, policy_holder_relationship="Self"),
            "policy-holder relationship",
        )
        staff = {
            "registration_type": "STAFF",
            "payer_type": "Staff",
            "payer_name": "POH staff — dependant",
            "employee_id": "EMP-221",
        }
        self.assert_rejected(
            self.body(**staff, policy_holder_relationship="Self"),
            "policy-holder relationship",
        )
        self.assert_rejected(
            self.child_body(**{**staff, "payer_name": "POH staff — self"}),
            "never staff self",
        )
        card = stage_document(self.user, "payer_card")
        response = self.register(
            self.child_body(
                documents=[card], **staff, policy_holder_relationship="Child"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            PatientRegistration.objects.get().policy_holder_relationship, "Child"
        )

    def test_digital_arrival_is_not_a_counter_source(self):
        self.assert_rejected(
            self.body(arrival_source="Digital / app", lead_source="POH app"),
            "is not one of",
        )
        self.assert_rejected(
            self.body(arrival_source="Unknown source"), "is not one of"
        )

    def test_patient_rejected_by_care_leaves_no_registration(self):
        self.assert_rejected(
            self.body(
                patient={"date_of_birth": datetime.date(1890, 1, 1).isoformat()},
                pregnancy_status="",
            ),
            "1900",
        )

    def test_detail_needs_patient_access(self):
        self.register(self.body())
        patient = Patient.objects.get()
        self.client.force_authenticate(user=self.create_user())
        response = self.client.get(
            reverse("desk-registration-detail", args=[patient.external_id])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update(self, patient, body):
        return self.client.put(
            reverse("desk-registration-detail", args=[patient.external_id]),
            body,
            format="json",
        )

    def test_update_edits_patient_and_registration_and_keeps_uhid(self):
        self.register(
            self.body(
                is_vip=True, vip_note="Minister", vip_authorised_by="Medical Director"
            )
        )
        patient = Patient.objects.get()
        uhid = patient.instance_identifiers
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.vip_flagged_by, self.user)
        body = self.body(
            patient={"name": "Neha Verma", "address": "D-4, Ring Road"},
            last_name="Verma",
            house="D-4",
            allergy_status="nka",
            allergies=[],
            is_vip=True,
            vip_note="Minister",
            vip_authorised_by="Medical Director",
        )
        editor = self.create_super_user(username="editor")
        self.client.force_authenticate(user=editor)
        response = self.update(patient, body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(patient.name, "Neha Verma")
        self.assertEqual(patient.instance_identifiers, uhid)
        self.assertEqual(record.last_name, "Verma")
        self.assertEqual(record.house, "D-4")
        self.assertEqual(record.allergies.count(), 0)
        # The original flagger is kept while the patient stays VIP.
        self.assertEqual(record.vip_flagged_by, self.user)
        self.assertEqual(record.updated_by, editor)
        self.assertEqual(PatientRegistration.objects.count(), 1)

        response = self.update(patient, self.body(allergy_status="nka", allergies=[]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record.refresh_from_db()
        self.assertIsNone(record.vip_flagged_by)

    def test_update_applies_registration_rules(self):
        self.register(self.body())
        patient = Patient.objects.get()
        response = self.update(patient, self.body(first_name="N"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first name", str(response.data))
        response = self.update(patient, {**self.body(), "pre_registration": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            PatientRegistration.objects.get(patient=patient).first_name, "Neha"
        )

    def test_update_counts_documents_already_on_the_patient(self):
        corporate = {
            "registration_type": "CORP",
            "payer_type": "Corporate",
            "payer_name": "Infosys",
            "employee_id": "INF-2231",
            "policy_holder_relationship": "Self",
        }
        self.register(self.body())
        patient = Patient.objects.get()
        response = self.update(patient, self.body(**corporate))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("the payer card", str(response.data))
        letter = stage_document(self.user, "payer_card", "single")
        response = self.update(patient, self.body(documents=[letter], **corporate))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(PatientDocument.objects.get().patient, patient)
        # The card is on the record now, so later edits don't need it again.
        response = self.update(patient, self.body(remarks="Edited", **corporate))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_update_creates_a_missing_registration(self):
        self.register(self.body())
        patient = Patient.objects.get()
        PatientRegistration.objects.all().delete()
        response = self.update(patient, self.body(allergy_status="nka", allergies=[]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.facility, self.facility)
        self.assertEqual(record.created_by, self.user)

    def test_update_needs_write_access(self):
        self.register(self.body())
        patient = Patient.objects.get()
        self.client.force_authenticate(user=self.create_user())
        response = self.update(patient, self.body(remarks="Edited"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def no_phone_body(self, **registration):
        return self.body(
            patient={"phone_number": None},
            **{
                "mobile_owner": "No phone",
                "whatsapp_number": None,
                "communication_channels": [],
                **registration,
            },
        )

    def create_no_phone(self, body=None):
        EmergencyDesk.objects.get_or_create(
            facility=self.facility, defaults={"phone_number": "+919876543210"}
        )
        response = self.register(body or self.no_phone_body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return Patient.objects.get(external_id=response.data["patient"]["id"]), response

    def test_no_phone_uses_desk_fallback_and_normal_uhid(self):
        patient, response = self.create_no_phone()
        self.assertEqual(patient.phone_number, "+919876543210")
        self.assertEqual(response.data["patient"]["phone_number"], "")
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.mobile_owner, "No phone")
        self.assertEqual(record.whatsapp_number, "")
        self.assertEqual(record.communication_channels, [])
        number = next(
            item["value"]
            for item in response.data["patient"]["instance_identifiers"]
            if item["config"]["config"]["system"] == "care_emergency/uhid"
        )
        self.assertTrue(number.startswith("POH-LN-"))
        lookup = self.client.get(
            reverse("patient-number-search"), {"uhid": number.lower()}
        )
        self.assertEqual(lookup.status_code, 200, lookup.data)
        self.assertEqual(lookup.data["results"][0]["id"], str(patient.external_id))
        self.assertEqual(lookup.data["results"][0]["phone_number"], "")
        detail = self.client.get(f"/api/v1/patient/{patient.external_id}/")
        self.assertEqual(detail.data["phone_number"], "")
        listing = self.client.get("/api/v1/patient/")
        self.assertEqual(listing.data["results"][0]["phone_number"], "")
        self.assertFalse(patient.emergency_encounters.exists())

    def test_no_phone_missing_configuration_and_conflicts_roll_back(self):
        self.assert_rejected(self.no_phone_body(), "desk number")
        body = self.no_phone_body()
        body["facility"] = None
        self.assert_rejected(body, "needs a facility")
        for changes in (
            {"whatsapp_number": PHONE},
            {"alternate_phone_number": PHONE},
            {"communication_channels": ["SMS"]},
            {"communication_channels": ["Email"]},
        ):
            with self.subTest(changes=changes):
                self.assert_rejected(
                    self.no_phone_body(**changes),
                    "No phone"
                    if "Email" not in changes.get("communication_channels", [])
                    else "email address",
                )
        body = self.no_phone_body()
        body["patient"]["phone_number"] = PHONE
        self.assert_rejected(body, "No phone")

    def test_minor_no_phone_keeps_guardian_without_requiring_a_number(self):
        body = self.child_body(
            mobile_owner="No phone",
            whatsapp_number=None,
            communication_channels=[],
            next_of_kin_phone=None,
        )
        body["patient"]["phone_number"] = None
        patient, _ = self.create_no_phone(body)
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.guardian_name, "Rakesh Sharma")
        self.assertEqual(record.next_of_kin_relationship, "Father")
        self.assertEqual(record.next_of_kin_phone, "")

    def test_multiple_no_phone_patients_have_distinct_uhids(self):
        first, response = self.create_no_phone()
        second, another = self.create_no_phone()
        self.assertNotEqual(first.external_id, second.external_id)
        self.assertEqual(first.phone_number, second.phone_number)
        self.assertNotEqual(
            response.data["patient"]["instance_identifiers"],
            another.data["patient"]["instance_identifiers"],
        )

    def test_add_contact_preserves_uhid_and_audits_owner(self):
        patient, response = self.create_no_phone()
        identifiers = response.data["patient"]["instance_identifiers"]
        preserved = {
            field: response.data["patient"][field]
            for field in (
                "name",
                "gender",
                "date_of_birth",
                "address",
                "pincode",
                "blood_group",
            )
        }
        url = reverse("patient-contact", args=[patient.external_id])
        for phone in ("+919876543210", "+915123456789", "+447911123456"):
            rejected = self.client.patch(
                url, {"phone_number": phone, "mobile_owner": "Relative"}, format="json"
            )
            self.assertEqual(rejected.status_code, 400, rejected.data)
            patient.refresh_from_db()
            self.assertEqual(patient.phone_number, "+919876543210")
        updated = self.client.patch(
            url, {"phone_number": PHONE, "mobile_owner": "Relative"}, format="json"
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["phone_number"], PHONE)
        self.assertEqual(updated.data["instance_identifiers"], identifiers)
        self.assertEqual({field: updated.data[field] for field in preserved}, preserved)
        record = PatientRegistration.objects.get(patient=patient)
        self.assertEqual(record.mobile_owner, "Relative")
        self.assertEqual(record.updated_by, self.user)
        self.assertEqual(
            self.client.get(f"/api/v1/patient/{patient.external_id}/").data[
                "phone_number"
            ],
            PHONE,
        )
        self.assertEqual(
            self.client.patch(
                url, {"phone_number": PHONE, "mobile_owner": "Self"}, format="json"
            ).status_code,
            400,
        )

    def test_no_phone_permissions_for_lookup_and_contact(self):
        patient, response = self.create_no_phone()
        number = response.data["patient"]["instance_identifiers"][0]["value"]
        with mock.patch("care_emergency.views.can", return_value=False):
            lookup = self.client.get(reverse("patient-number-search"), {"uhid": number})
            self.assertEqual(lookup.data["results"], [])
            update = self.client.patch(
                reverse("patient-contact", args=[patient.external_id]),
                {"phone_number": PHONE, "mobile_owner": "Self"},
                format="json",
            )
            self.assertEqual(update.status_code, 403)
            rejected = self.register(self.no_phone_body())
            self.assertEqual(rejected.status_code, 403)
        patient.refresh_from_db()
        self.assertEqual(patient.phone_number, "+919876543210")
        self.assertEqual(Patient.objects.count(), 1)

    def test_shared_desk_number_cannot_be_used_for_patient_otp_or_token(self):
        from config.patient_otp_authentication import JWTTokenPatientAuthentication
        from rest_framework_simplejwt.exceptions import InvalidToken

        self.create_no_phone()
        self.client.force_authenticate(user=None)
        for endpoint, data in (
            ("otp-login-send", {"phone_number": "+919876543210"}),
            ("otp-login-login", {"phone_number": "+919876543210", "otp": "45612"}),
        ):
            with (
                self.subTest(endpoint=endpoint),
                mock.patch(
                    "care.emr.api.otp_viewsets.login.sms.send_text_message"
                ) as send,
            ):
                rejected = self.client.post(reverse(endpoint), data, format="json")
                self.assertEqual(rejected.status_code, 400, rejected.data)
                send.assert_not_called()
        with self.assertRaises(InvalidToken):
            JWTTokenPatientAuthentication().get_user({"phone_number": "+919876543210"})
        self.assertEqual(
            JWTTokenPatientAuthentication()
            .get_user({"phone_number": PHONE})
            .phone_number,
            PHONE,
        )

    def test_minor_contact_owner_and_number_are_required_after_no_phone(self):
        body = self.child_body(
            mobile_owner="No phone",
            whatsapp_number=None,
            communication_channels=[],
            next_of_kin_phone=None,
        )
        body["patient"]["phone_number"] = None
        patient, _ = self.create_no_phone(body)
        url = reverse("patient-contact", args=[patient.external_id])
        for owner in ("Self", "No phone", "Cousin"):
            response = self.client.patch(
                url, {"phone_number": PHONE, "mobile_owner": owner}, format="json"
            )
            self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(
            url, {"phone_number": PHONE, "mobile_owner": "Parent"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            PatientRegistration.objects.get(patient=patient).mobile_owner, "Parent"
        )

    def test_no_phone_can_use_email_without_enabling_mobile_channels(self):
        patient, _ = self.create_no_phone(
            self.no_phone_body(
                email="neha@example.com", communication_channels=["Email"]
            )
        )
        self.assertEqual(
            PatientRegistration.objects.get(patient=patient).communication_channels,
            ["Email"],
        )

    def test_old_fallback_stays_blocked_after_desk_number_changes(self):
        from care.emr.resources.patient.hooks import patient_phone_allowed

        self.create_no_phone()
        EmergencyDesk.objects.filter(facility=self.facility).update(
            phone_number="+919876543211"
        )
        self.assertFalse(patient_phone_allowed("+919876543210"))
        self.assertFalse(patient_phone_allowed("+919876543211"))
        self.assertTrue(patient_phone_allowed(PHONE))

    def test_desk_staff_can_register_and_lookup_without_encounter_permissions(self):
        desk = self.create_user()
        org = self.create_facility_organization(facility=self.facility)
        self.attach_role_facility_organization_user(
            org,
            desk,
            self.create_role_with_permissions(
                ["can_create_patient", "can_list_patients", "can_write_patient"]
            ),
        )
        self.client.force_authenticate(user=desk)
        patient, response = self.create_no_phone()
        self.assertFalse(patient.emergency_encounters.exists())
        self.assertEqual(
            self.client.get(f"/api/v1/patient/{patient.external_id}/").status_code, 200
        )
        number = response.data["patient"]["instance_identifiers"][0]["value"]
        lookup = self.client.get(reverse("patient-number-search"), {"uhid": number})
        self.assertEqual(lookup.data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/patient/").data["count"], 1)
        updated = self.client.patch(
            reverse("patient-contact", args=[patient.external_id]),
            {"phone_number": PHONE, "mobile_owner": "Self"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        # The registration facility remains the association after adding a phone.
        self.assertEqual(
            self.client.get(reverse("patient-number-search"), {"uhid": number}).data[
                "count"
            ],
            1,
        )
        outsider = self.create_user()
        self.client.force_authenticate(user=outsider)
        self.assertEqual(
            self.client.get(reverse("patient-number-search"), {"uhid": number}).data[
                "count"
            ],
            0,
        )
        self.assertEqual(self.client.get("/api/v1/patient/").data["count"], 0)
        self.assertEqual(
            self.client.get(f"/api/v1/patient/{patient.external_id}/").status_code, 404
        )

    def test_no_phone_cannot_borrow_another_facility_desk(self):
        EmergencyDesk.objects.create(
            facility=self.facility, phone_number="+919876543210"
        )
        other_user = self.create_user()
        other_facility = self.create_facility(user=other_user)
        org = self.create_facility_organization(facility=other_facility)
        self.attach_role_facility_organization_user(
            org,
            other_user,
            self.create_role_with_permissions(
                ["can_create_patient", "can_list_patients"]
            ),
        )
        self.client.force_authenticate(user=other_user)
        rejected = self.register(self.no_phone_body())
        self.assertEqual(rejected.status_code, 403, rejected.data)
        self.assertFalse(Patient.objects.exists())
        self.assertFalse(PatientRegistration.objects.exists())
