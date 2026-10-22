import datetime

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import (
    Availability,
    Patient,
    PatientUser,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.emr.models.organization import FacilityOrganizationUser
from care.utils.tests.base import CareAPITestBase
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.app_server import setup_app_server
from care_emergency.constants import APP_SERVER_ROLE
from care_emergency.models import AppPatientLink, AppointmentDetails, FacilityUnit

# Clearly fake people.
PHONE = "+919800000001"
ABHA = "91000000000001"


def local(day, hour, minute=0):
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


class AppServerTests(CareAPITestBase):
    @classmethod
    def setUpTestData(cls):
        # The test database starts without Care's permissions.
        call_command("sync_permissions_roles")

    def setUp(self):
        super().setUp()
        PatientCreateLock().release()
        self.admin = self.create_super_user(username="desk-admin")
        self.facility = self.create_facility(user=self.admin)
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        self.app, _ = setup_app_server()
        self.client.force_authenticate(user=self.app)

        self.doctor = self.create_user(username="dr-test")
        self.tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        resource = SchedulableResource.objects.create(
            facility=self.facility, resource_type="practitioner", user=self.doctor
        )
        schedule = Schedule.objects.create(
            resource=resource,
            name="OPD",
            valid_from=timezone.now() - datetime.timedelta(days=30),
            valid_to=timezone.now() + datetime.timedelta(days=30),
        )
        availability = Availability.objects.create(
            schedule=schedule,
            name="Morning",
            slot_type="appointment",
            slot_size_in_minutes=15,
            tokens_per_slot=1,
            availability=[
                {
                    "day_of_week": self.tomorrow.weekday(),
                    "start_time": "09:00:00",
                    "end_time": "10:00:00",
                }
            ],
        )
        self.slots = [
            TokenSlot.objects.create(
                resource=resource,
                availability=availability,
                start_datetime=local(self.tomorrow, 9, minute),
                end_datetime=local(self.tomorrow, 9, minute + 15),
            )
            for minute in (0, 15, 30)
        ]

    def profile(self, **kwargs):
        return {
            "app_user_id": "app-user-1",
            "facility": str(self.facility.external_id),
            "title": "Ms",
            "first_name": "Test",
            "last_name": "Person",
            "date_of_birth": "1990-01-01",
            "gender": "female",
            "phone_number": PHONE,
            **kwargs,
        }

    def register(self, **kwargs):
        PatientCreateLock().release()
        return self.client.post(
            reverse("app-patients"), self.profile(**kwargs), format="json"
        )

    def book(self, patient_id, slot=0, **kwargs):
        return self.client.post(
            reverse("desk-appointment-list"),
            {
                "slot": str(self.slots[slot].external_id),
                "patient": patient_id,
                "booking_channel": "app",
                **kwargs,
            },
            format="json",
        )

    def test_setup_is_idempotent_and_covers_new_facilities(self):
        self.assertTrue(self.app.is_service_account)
        self.assertFalse(self.app.has_usable_password())
        self.assertFalse(self.app.is_superuser)
        user, granted = setup_app_server()
        self.assertEqual((user, granted), (self.app, 0))
        self.facility.refresh_from_db()
        root = FacilityOrganizationUser.objects.get(
            user=self.app, organization=self.facility.default_internal_organization
        )
        self.assertEqual(root.role.name, APP_SERVER_ROLE)

        other = self.create_facility(user=self.admin)
        self.assertTrue(
            FacilityOrganizationUser.objects.filter(
                user=self.app, organization=other.default_internal_organization
            ).exists()
        )

    def test_register_creates_pre_registered_patient_once(self):
        response = self.register(abha_number=ABHA, identity_storage_consent=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        body = response.json()
        self.assertTrue(body["pre_registration_number"].startswith("PRE-LN-"))
        self.assertIsNone(body["uhid"])
        self.assertFalse(body["matched"])
        patient = Patient.objects.get(external_id=body["patient"]["id"])
        self.assertEqual(patient.name, "Test Person")
        self.assertEqual(patient.meta["registration"]["abha_number"], ABHA)
        self.assertTrue(
            PatientUser.objects.filter(patient=patient, user=self.app).exists()
        )

        again = self.register(
            first_name="Tested", abha_number=ABHA, identity_storage_consent=True
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK, again.data)
        self.assertEqual(again.json()["patient"]["id"], str(patient.external_id))
        self.assertEqual(
            again.json()["pre_registration_number"], body["pre_registration_number"]
        )
        patient.refresh_from_db()
        self.assertEqual(patient.name, "Tested Person")
        self.assertEqual(Patient.objects.count(), 1)

        detail = self.client.get(reverse("app-patient", args=["app-user-1"]))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.json()["patient"]["id"], str(patient.external_id))
        # Care's own patient API serves it too, through the PatientUser link.
        care = self.client.get(f"/api/v1/patient/{patient.external_id}/")
        self.assertEqual(care.status_code, status.HTTP_200_OK)

    def test_register_validation_and_who_may_call(self):
        response = self.register(abha_number=ABHA)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Consent", str(response.data))
        response = self.register(title="Mr")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.force_authenticate(user=self.admin)
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Patient.objects.count(), 0)

    def test_abha_links_one_existing_patient_without_changing_it(self):
        existing = self.create_patient(
            name="Desk Patient",
            phone_number=PHONE,
            meta={"registration": {"abha_number": ABHA}},
        )
        response = self.register(abha_number=ABHA, identity_storage_consent=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.json()["matched"])
        self.assertEqual(response.json()["patient"]["id"], str(existing.external_id))

        self.register(
            first_name="Changed", abha_number=ABHA, identity_storage_consent=True
        )
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Desk Patient")

        # Two patients share the number: no guess, a new patient.
        self.create_patient(
            phone_number=PHONE, meta={"registration": {"abha_number": "91000000000002"}}
        )
        self.create_patient(
            phone_number=PHONE, meta={"registration": {"abha_number": "91000000000002"}}
        )
        response = self.register(
            app_user_id="app-user-2",
            abha_number="91000000000002",
            identity_storage_consent=True,
        )
        self.assertFalse(response.json()["matched"])
        self.assertEqual(Patient.objects.count(), 4)

    def test_app_books_edits_cancels_and_reschedules_its_own_patients(self):
        patient_id = self.register().json()["patient"]["id"]
        stranger = self.create_patient(phone_number="+919800000002")

        refused = self.book(str(stranger.external_id))
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)

        booked = self.book(patient_id, note="From the app")
        self.assertEqual(booked.status_code, status.HTTP_200_OK, booked.data)
        details = booked.json()["details"]
        self.assertEqual(details["booking_channel"], "app")
        self.assertEqual(details["status"], "booked")
        booking_id = details["booking"]
        detail_url = reverse("desk-appointment-detail", args=[booking_id])

        edited = self.client.patch(
            detail_url,
            {"consultation_type": "video", "note": "Video please"},
            format="json",
        )
        self.assertEqual(edited.status_code, status.HTTP_200_OK, edited.data)
        self.assertEqual(edited.json()["consultation_type"], "video")
        self.assertEqual(
            TokenBooking.objects.get(external_id=booking_id).note, "Video please"
        )

        moved = self.client.post(
            reverse("desk-appointment-reschedule", args=[booking_id]),
            {"slot": str(self.slots[1].external_id)},
            format="json",
        )
        self.assertEqual(moved.status_code, status.HTTP_200_OK, moved.data)
        new_id = moved.json()["details"]["booking"]
        self.assertNotEqual(new_id, booking_id)
        self.assertEqual(
            moved.json()["details"]["appointment_number"], details["appointment_number"]
        )
        self.assertEqual(
            TokenBooking.objects.get(external_id=booking_id).status, "rescheduled"
        )
        for slot in self.slots[:2]:
            slot.refresh_from_db()
        self.assertEqual((self.slots[0].allocated, self.slots[1].allocated), (0, 1))

        cancelled = self.client.post(
            reverse("desk-appointment-cancel", args=[new_id]),
            {"note": "Can't come"},
            format="json",
        )
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK, cancelled.data)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.slots[1].refresh_from_db()
        self.assertEqual(self.slots[1].allocated, 0)
        again = self.client.post(
            reverse("desk-appointment-cancel", args=[new_id]), {}, format="json"
        )
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AppointmentDetails.objects.count(), 1)

    def test_app_sees_only_its_patients_bookings(self):
        patient_id = self.register().json()["patient"]["id"]
        self.book(patient_id)
        stranger = self.create_patient(phone_number="+919800000002")
        self.client.force_authenticate(user=self.admin)
        other = self.book(
            str(stranger.external_id), slot=2, booking_channel="front_desk"
        )
        self.assertEqual(other.status_code, status.HTTP_200_OK, other.data)
        other_id = other.json()["details"]["booking"]
        self.client.force_authenticate(user=self.app)

        listed = self.client.get(reverse("desk-appointment-list"))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([x["patient"] for x in listed.json()["results"]], [patient_id])
        self.assertEqual(
            self.client.get(
                reverse("desk-appointment-detail", args=[other_id])
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                reverse("desk-appointment-cancel", args=[other_id])
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

        care = self.client.get(
            f"/api/v1/facility/{self.facility.external_id}/appointments/",
            {"resource_type": "practitioner"},
        )
        self.assertEqual(care.status_code, status.HTTP_200_OK, care.data)
        self.assertEqual(
            [x["patient"]["id"] for x in care.json()["results"]], [patient_id]
        )
        self.assertEqual(
            self.client.get(f"/api/v1/patient/{stranger.external_id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(AppPatientLink.objects.count(), 1)
