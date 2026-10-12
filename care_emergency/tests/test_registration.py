import datetime

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.models import FacilityUnit, PatientRegistration

PHONE = "+919812345678"


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

    def body(self, patient=None, **registration):
        return {
            "facility": str(self.facility.external_id),
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
                patient={"phone_number": "+447911123456"},
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
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = PatientRegistration.objects.get()
        self.assertEqual(record.interpreter_language, "Arabic")
        self.assertEqual(record.frro_c_form, "Auto-file within 24 h")
        self.assertTrue(record.is_vip)
        self.assertEqual(record.vip_flagged_by, self.user)

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
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

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
