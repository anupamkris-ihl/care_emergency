import re

from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from rest_framework import status

from care_emergency.models import BreakGlassAccess, RestrictedPatient, StatutoryFlag

ALIAS = re.compile(r"^PT-[2-9A-HJ-NP-Z]{6}$")


class RestrictedPatientTests(CareAPITestBase):
    """D30: a clinician's statutory flag hides the patient's identity from
    everyone else, the front desk included, until they break the glass."""

    def setUp(self):
        super().setUp()
        admin = self.create_super_user()
        self.admin = admin
        self.facility = self.create_facility(user=admin)
        organization = self.create_facility_organization(facility=self.facility)
        self.patient = self.create_patient(
            name="Meera Iyer",
            phone_number="+919812345678",
            address="12 MG Road",
            pincode=560001,
        )
        self.encounter = self.create_encounter(
            self.patient, self.facility, organization
        )
        self.doctor = self.create_user()
        self.attach_role_facility_organization_user(
            organization,
            self.doctor,
            self.create_role_with_permissions(
                [
                    "can_list_patients",
                    "can_view_clinical_data",
                    "can_write_service_request",
                ]
            ),
        )
        self.desk = self.create_user()
        self.attach_role_facility_organization_user(
            organization,
            self.desk,
            self.create_role_with_permissions(
                [
                    "can_list_patients",
                    "can_view_clinical_data",
                    "can_create_patient",
                    "can_write_patient",
                ]
            ),
        )

    def flag(self, user, category="mtp"):
        self.client.force_authenticate(user=user)
        return self.client.post(
            reverse("patient-statutory-flags", args=[self.patient.external_id]),
            {"encounter": str(self.encounter.external_id), "category": category},
            format="json",
        )

    def patient_as(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(f"/api/v1/patient/{self.patient.external_id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def restriction_as(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(
            reverse("patient-restriction", args=[self.patient.external_id])
        ).data

    def test_front_desk_cannot_raise_a_flag(self):
        self.assertEqual(self.flag(self.desk).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(RestrictedPatient.objects.exists())

    def test_flag_masks_the_patient_for_everyone_but_the_clinician(self):
        self.assertEqual(self.patient_as(self.desk)["name"], "Meera Iyer")
        response = self.flag(self.doctor)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        alias = response.data["alias"]
        self.assertRegex(alias, ALIAS)
        self.assertEqual(response.data["categories"], ["mtp"])
        # A second flag keeps the alias.
        self.assertEqual(self.flag(self.doctor, "hiv").data["alias"], alias)
        self.assertEqual(StatutoryFlag.objects.count(), 2)

        masked = self.patient_as(self.desk)
        self.assertEqual(masked["name"], alias)
        self.assertEqual(masked["phone_number"], "Restricted")
        self.assertEqual(masked["address"], "Restricted")
        self.assertIsNone(masked["pincode"])
        self.assertIsNone(masked["date_of_birth"])
        self.assertIsNone(masked["year_of_birth"])
        self.assertEqual(self.patient_as(self.doctor)["name"], "Meera Iyer")

        seen = self.restriction_as(self.desk)
        self.assertEqual(
            seen, {"restricted": True, "alias": alias, "can_see_identity": False}
        )
        # The record itself is unchanged.
        self.assertEqual(Patient.objects.get().name, "Meera Iyer")

    def test_break_glass_unmasks_for_an_hour_with_a_reason(self):
        self.flag(self.doctor)
        self.client.force_authenticate(user=self.desk)
        url = reverse("patient-break-glass", args=[self.patient.external_id])
        short = self.client.post(url, {"reason": "need"}, format="json")
        self.assertEqual(short.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(
            url, {"reason": "Patient collapsed at the counter"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["break_glass_until"])
        self.assertEqual(self.patient_as(self.desk)["name"], "Meera Iyer")
        access = BreakGlassAccess.objects.get()
        self.assertEqual(access.user, self.desk)
        self.assertEqual(access.reason, "Patient collapsed at the counter")

        access.expires_at = access.created_date
        access.save()
        self.assertNotEqual(self.patient_as(self.desk)["name"], "Meera Iyer")

    def test_break_glass_needs_a_restricted_patient(self):
        self.client.force_authenticate(user=self.desk)
        response = self.client.post(
            reverse("patient-break-glass", args=[self.patient.external_id]),
            {"reason": "Checking the record"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.restriction_as(self.desk), {"restricted": False})

    def test_only_clinicians_receive_flaggable_encounters(self):
        url = reverse("patient-statutory-flags", args=[self.patient.external_id])
        self.client.force_authenticate(user=self.desk)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
        self.client.force_authenticate(user=self.doctor)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [x["id"] for x in response.data], [str(self.encounter.external_id)]
        )

    def test_closed_encounters_are_not_flaggable(self):
        url = reverse("patient-statutory-flags", args=[self.patient.external_id])
        for encounter_status in [
            "completed",
            "discharged",
            "cancelled",
            "discontinued",
            "entered_in_error",
        ]:
            with self.subTest(status=encounter_status):
                self.encounter.status = encounter_status
                self.encounter.save()
                self.assertIn(self.flag(self.doctor).status_code, [400, 403])
                self.client.force_authenticate(user=self.admin)
                self.assertEqual(self.client.get(url).data, [])
        self.assertFalse(RestrictedPatient.objects.exists())
