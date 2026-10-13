from io import StringIO

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.constants import PatientNumberSeries
from care_emergency.models import EmergencyDesk, FacilityUnit
from care_emergency.services import patient_number, patient_number_config
from care_emergency.tests.test_registration import PHONE, registration_details

DESK_NUMBER = "+919876543210"


class PatientNumberTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        EmergencyDesk.objects.create(facility=self.facility, phone_number=DESK_NUMBER)
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()

    def post(self, name, body, **kwargs):
        # Care releases its create locks on commit; run those callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(name, kwargs=kwargs), body, format="json")

    def register_at_desk(self, **kwargs):
        return self.post(
            "desk-registration",
            {
                "facility": str(self.facility.external_id),
                "patient": {
                    "name": "Neha Sharma",
                    "gender": "female",
                    "phone_number": PHONE,
                    "date_of_birth": "1999-04-12",
                    "address": "C-22, Ring Road, Lajpat Nagar III, Delhi",
                    "pincode": 110024,
                },
                "registration": registration_details(),
                **kwargs,
            },
        )

    def register_emergency(self, **kwargs):
        return self.post(
            "emergency-register",
            {
                "facility": str(self.facility.external_id),
                "patient": {"name": "Unknown Male", "gender": "male", "age": 35},
                "triage": "red",
                "is_medico_legal": False,
                "is_unidentified": True,
                **kwargs,
            },
        )

    def numbers(self, response):
        return [x["value"] for x in response.data["patient"]["instance_identifiers"]]

    def test_desk_registration_issues_sequential_uhids(self):
        first = self.register_at_desk()
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        self.assertIn("POH-LN-0000001", self.numbers(first))
        PatientCreateLock().release()
        second = self.register_at_desk()
        self.assertIn("POH-LN-0000002", self.numbers(second))
        config = patient_number_config(PatientNumberSeries.uhid)
        self.assertTrue(config.config["auto_maintained"])
        self.assertEqual(config.config["display"], "UHID")

    def test_uhid_cant_be_edited_through_care(self):
        patient_id = self.register_at_desk().data["patient"]["id"]
        config = patient_number_config(PatientNumberSeries.uhid)
        response = self.client.post(
            f"/api/v1/patient/{patient_id}/update_identifier/",
            {"config": str(config.external_id), "value": "POH-LN-0000099"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_registration_needs_unit_code(self):
        FacilityUnit.objects.all().delete()
        response = self.register_at_desk()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unit code", str(response.data))
        self.assertFalse(Patient.objects.exists())

    def test_pre_registration_converts_on_arrival(self):
        response = self.register_at_desk(pre_registration=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.numbers(response), ["PRE-LN-0000001"])
        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.create_encounter(patient, self.facility, self.organization)
        self.assertEqual(
            patient_number(patient, PatientNumberSeries.uhid), "POH-LN-0000001"
        )
        self.assertEqual(
            patient_number(patient, PatientNumberSeries.pre_registration),
            "PRE-LN-0000001",
        )

    def test_pre_registration_converts_on_payment(self):
        patient_id = self.register_at_desk(pre_registration=True).data["patient"]["id"]
        body = {"facility": str(self.facility.external_id)}
        response = self.post("patient-uhid", body, patient_id=patient_id)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(x["value"] for x in response.data["instance_identifiers"]),
            ["POH-LN-0000001", "PRE-LN-0000001"],
        )
        # Issuing again keeps the same number.
        again = self.post("patient-uhid", body, patient_id=patient_id)
        self.assertEqual(
            sorted(x["value"] for x in again.data["instance_identifiers"]),
            ["POH-LN-0000001", "PRE-LN-0000001"],
        )

    def test_unknown_patients_are_numbered_per_day(self):
        day = timezone.localdate().strftime("%Y%m%d")
        first = self.register_emergency()
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        self.assertEqual(first.data["patient"]["name"], "Unknown 1")
        self.assertEqual(self.numbers(first), [f"UNK-LN-{day}-001"])
        PatientCreateLock().release()
        second = self.register_emergency()
        self.assertEqual(second.data["patient"]["name"], "Unknown 2")
        self.assertEqual(self.numbers(second), [f"UNK-LN-{day}-002"])

    def test_unknown_patient_gets_uhid_on_conversion(self):
        day = timezone.localdate().strftime("%Y%m%d")
        patient_id = self.register_emergency().data["patient"]["id"]
        blocked = self.post(
            "patient-uhid",
            {"facility": str(self.facility.external_id)},
            patient_id=patient_id,
        )
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.post(
            "emergency-convert",
            {"name": "Arun Kumar", "phone_number": "+919812345670"},
            patient_id=patient_id,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(self.numbers(response)), ["POH-LN-0000001", f"UNK-LN-{day}-001"]
        )

    def test_identified_emergency_patient_gets_uhid(self):
        response = self.register_emergency(
            patient={"name": "Arun Kumar", "gender": "male", "age": 35},
            is_unidentified=False,
            chief_complaint="Fall",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["patient"]["name"], "Arun Kumar")
        self.assertEqual(self.numbers(response), ["POH-LN-0000001"])

    def test_unit_code_is_set_once(self):
        other = self.create_facility(user=self.user)
        url = reverse("facility-unit")
        response = self.client.put(
            url, {"facility": str(other.external_id), "code": "KB"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["code"], "KB")
        cases = [
            ("DW", "can't be changed"),
            ("kb1", "should match pattern"),
        ]
        for code, message in cases:
            with self.subTest(code=code):
                response = self.client.put(
                    url,
                    {"facility": str(other.external_id), "code": code},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(message, str(response.data))
        third = self.create_facility(user=self.user)
        response = self.client.put(
            url, {"facility": str(third.external_id), "code": "LN"}, format="json"
        )
        self.assertIn("already uses", str(response.data))

    def test_backfill_numbers_existing_patients(self):
        older = self.create_patient(name="Older")
        visited = self.create_patient(name="Visited")
        other = self.create_facility(user=self.user)
        FacilityUnit.objects.create(facility=other, code="KB")
        self.create_encounter(
            visited, other, self.create_facility_organization(facility=other)
        )
        # The encounter above already issued Visited's UHID; clear it to backfill.
        visited.patientidentifier_set.all().delete()
        orphan = self.create_patient(name="Orphan")
        call_command(
            "issue_patient_numbers",
            default_facility=str(self.facility.external_id),
            stdout=StringIO(),
        )
        self.assertEqual(
            patient_number(older, PatientNumberSeries.uhid), "POH-LN-0000001"
        )
        self.assertEqual(
            patient_number(visited, PatientNumberSeries.uhid), "POH-KB-0000002"
        )
        self.assertEqual(
            patient_number(orphan, PatientNumberSeries.uhid), "POH-LN-0000002"
        )
        # Running again changes nothing.
        call_command("issue_patient_numbers", stdout=StringIO())
        self.assertEqual(
            patient_number(older, PatientNumberSeries.uhid), "POH-LN-0000001"
        )
