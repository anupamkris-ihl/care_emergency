from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Encounter, Patient
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from rest_framework import status

from care_emergency.models import EmergencyDesk, EmergencyEncounter

DESK_NUMBER = "+919876543210"
REAL_NUMBER = "+919812345678"
COMPANION_NUMBER = "+919898989898"


class EmergencyAPITests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()

    def set_desk(self):
        return EmergencyDesk.objects.create(
            facility=self.facility, phone_number=DESK_NUMBER
        )

    def register_data(self, **kwargs):
        data = {
            "facility": str(self.facility.external_id),
            "patient": {"name": "Unknown Male", "gender": "male", "age": 35},
            "triage": "red",
            "is_medico_legal": True,
            "mlc_nature": "Road traffic accident",
            "police_station": "Central",
            "distinguishing_marks": "Scar on left arm",
            "brought_by": {
                "name": "Ravi",
                "phone_number": COMPANION_NUMBER,
                "relationship": "Other",
            },
        }
        data.update(kwargs)
        return data

    def register(self, **kwargs):
        # Care releases its create locks on commit; run those callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("emergency-register"),
                self.register_data(**kwargs),
                format="json",
            )

    def test_register_requires_desk_number(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Patient.objects.exists())

    def test_register_creates_patient_encounter_and_mlc_number(self):
        self.set_desk()
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        emergency = response.data["emergency"]
        self.assertRegex(emergency["mlc_number"], r"^MLC-\d{4}-0001$")
        self.assertEqual(emergency["brought_by"]["phone_number"], COMPANION_NUMBER)

        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.assertEqual(patient.phone_number, DESK_NUMBER)
        encounter = Encounter.objects.get(external_id=response.data["encounter"]["id"])
        self.assertEqual(encounter.encounter_class, "emer")
        self.assertEqual(encounter.priority, "emergency")
        self.assertEqual(encounter.hospitalization["admit_source"], "emd")

    def test_mlc_numbers_are_sequential(self):
        self.set_desk()
        first = self.register().data["emergency"]["mlc_number"]
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        second = response.data["emergency"]["mlc_number"]
        self.assertTrue(first.endswith("0001"))
        self.assertTrue(second.endswith("0002"))

    def test_non_medico_legal_gets_no_mlc_number(self):
        self.set_desk()
        response = self.register(
            is_medico_legal=False, mlc_nature="Assault", police_station="X"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["emergency"]["mlc_number"])
        self.assertEqual(response.data["emergency"]["mlc_nature"], "")

    def test_medico_legal_requires_nature_and_police_station(self):
        self.set_desk()
        response = self.register(mlc_nature="", police_station="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_age_zero_is_rejected(self):
        self.set_desk()
        response = self.register(
            patient={"name": "Unknown Female", "gender": "female", "age": 0}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Patient.objects.exists())

    def test_register_without_permission(self):
        self.set_desk()
        self.client.force_authenticate(user=self.create_user())
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_can_clear_medico_legal(self):
        self.set_desk()
        encounter_id = self.register().data["encounter"]["id"]
        url = reverse(
            "emergency-encounter-detail", kwargs={"external_id": encounter_id}
        )
        response = self.client.patch(url, {"is_medico_legal": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["is_medico_legal"])
        # The issued number is kept so it is never reused.
        self.assertIsNotNone(response.data["mlc_number"])

    def test_list_filters(self):
        self.set_desk()
        self.register()
        self.register(is_medico_legal=False, triage="green")
        url = reverse("emergency-encounter-list")
        response = self.client.get(
            url, {"facility": str(self.facility.external_id), "is_medico_legal": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_convert_to_normal_patient(self):
        self.set_desk()
        patient_id = self.register().data["patient"]["id"]
        url = reverse("emergency-convert", kwargs={"patient_id": patient_id})
        response = self.client.post(
            url, {"name": "Arun Kumar", "phone_number": REAL_NUMBER}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient = Patient.objects.get(external_id=patient_id)
        self.assertEqual(patient.phone_number, REAL_NUMBER)
        self.assertEqual(patient.name, "Arun Kumar")
        record = EmergencyEncounter.objects.get(patient=patient)
        self.assertIsNotNone(record.converted_at)
        self.assertFalse(record.is_unidentified)

    def test_convert_rejects_desk_number(self):
        self.set_desk()
        patient_id = self.register().data["patient"]["id"]
        url = reverse("emergency-convert", kwargs={"patient_id": patient_id})
        response = self.client.post(url, {"phone_number": DESK_NUMBER}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(
            EmergencyEncounter.objects.get(patient__external_id=patient_id).converted_at
        )

    def test_desk_number_admin_only(self):
        url = reverse("emergency-desk")
        body = {"facility": str(self.facility.external_id), "phone_number": DESK_NUMBER}
        self.assertEqual(
            self.client.put(url, body, format="json").status_code, status.HTTP_200_OK
        )
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(
            self.client.put(url, body, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
