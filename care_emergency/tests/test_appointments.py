import datetime
from unittest import mock

from care.emr.models import (
    Availability,
    Encounter,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.models import AppointmentDetails, WalkInReserve, EmergencyDesk
from care_emergency.tasks import send_appointment_reminders


def local(day, hour, minute=0):
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


class AppointmentTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        self.patient = self.create_patient(phone_number="+919812345678")
        self.client.force_authenticate(user=self.user)
        self.today = timezone.localdate()
        self.tomorrow = self.today + datetime.timedelta(days=1)
        self.resource = SchedulableResource.objects.create(
            facility=self.facility, resource_type="practitioner", user=self.user
        )
        self.schedule = Schedule.objects.create(
            resource=self.resource,
            name="OPD",
            valid_from=timezone.now() - datetime.timedelta(days=30),
            valid_to=timezone.now() + datetime.timedelta(days=30),
        )
        # Tomorrow 09:00–10:00 in 15-minute slots of one seat: 4 seats.
        self.availability = Availability.objects.create(
            schedule=self.schedule,
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
            self.create_slot(local(self.tomorrow, 9, minute))
            for minute in (0, 15, 30, 45)
        ]

    def create_slot(self, start):
        return TokenSlot.objects.create(
            resource=self.resource,
            availability=self.availability,
            start_datetime=start,
            end_datetime=start + datetime.timedelta(minutes=15),
        )

    def book(self, slot=None, patient=None, **kwargs):
        body = {
            "slot": str((slot or self.slots[0]).external_id),
            "patient": str((patient or self.patient).external_id),
            "booking_channel": "front_desk",
            **kwargs,
        }
        return self.client.post(reverse("desk-appointment-list"), body, format="json")

    def test_booking_creates_care_booking_and_details(self):
        response = self.book(note="First visit")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking = TokenBooking.objects.get(external_id=response.data["booking"]["id"])
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.note, "First visit")
        self.slots[0].refresh_from_db()
        self.assertEqual(self.slots[0].allocated, 1)

        details = response.data["details"]
        year = timezone.now().year % 100
        self.assertEqual(details["appointment_number"], f"APT-{year:02d}-000001")
        self.assertEqual(details["consultation_type"], "in_person")
        self.assertEqual(details["encounter_class"], "amb")
        self.assertEqual(details["visit_purpose"], "new")
        self.assertIsNone(details["reminder_due_at"])

        detail = self.client.get(
            reverse("desk-appointment-detail", args=[booking.external_id])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(
            detail.data["appointment_number"], details["appointment_number"]
        )

        listed = self.client.get(
            reverse("desk-appointment-list"),
            {"facility": str(self.facility.external_id), "date": self.tomorrow},
        )
        self.assertEqual(listed.data["count"], 1)

    def test_guidance_is_kept_and_red_flags_go_to_emergency(self):
        response = self.book(
            booking_channel="front_desk",
            guidance_area="Stomach / digestion",
            guidance_since="4-7 days",
            chief_complaint="  Acidity after meals  ",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["details"]["chief_complaint"], "Acidity after meals"
        )
        details = AppointmentDetails.objects.get()
        self.assertEqual(details.guidance_area, "Stomach / digestion")
        self.assertEqual(details.guidance_since, "4-7 days")

        response = self.book(
            booking_channel="front_desk", red_flags=["Chest pain or pressure"]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("emergency desk", str(response.data))
        response = self.book(booking_channel="front_desk", guidance_area="Elbow")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AppointmentDetails.objects.count(), 1)

    def test_appointment_numbers_are_consecutive(self):
        other = self.create_patient(phone_number="+919812345679")
        first = self.book().data["details"]["appointment_number"]
        second = self.book(slot=self.slots[1], patient=other).data["details"][
            "appointment_number"
        ]
        self.assertEqual(int(second[-6:]), int(first[-6:]) + 1)

    def test_rejected_booking_leaves_no_details(self):
        self.slots[0].allocated = 1
        self.slots[0].save()
        response = self.book()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Slot is already full", str(response.data))
        self.assertFalse(AppointmentDetails.objects.exists())

    def test_visit_purpose_defaults_to_follow_up_inside_revisit_window(self):
        self.schedule.revisit_allowed_days = 7
        self.schedule.save()
        earlier = self.create_slot(local(self.today - datetime.timedelta(days=3), 9))
        TokenBooking.objects.create(
            token_slot=earlier, patient=self.patient, status="fulfilled"
        )
        response = self.book()
        self.assertEqual(response.data["details"]["visit_purpose"], "follow_up")

        other = self.create_patient(phone_number="+919812345679")
        response = self.book(slot=self.slots[1], patient=other)
        self.assertEqual(response.data["details"]["visit_purpose"], "new")

        response = self.book(
            slot=self.slots[2], patient=other, visit_purpose="report_review"
        )
        self.assertEqual(response.data["details"]["visit_purpose"], "report_review")

    def test_phone_booking_gets_reminder(self):
        response = self.book(booking_channel="phone")
        details = AppointmentDetails.objects.get(
            external_id=response.data["details"]["id"]
        )
        self.assertEqual(
            details.reminder_due_at,
            local(self.tomorrow, 9) - datetime.timedelta(hours=2),
        )

        with mock.patch("care_emergency.tasks.send_text_message") as send:
            send_appointment_reminders()
            send.assert_not_called()
            AppointmentDetails.objects.filter(pk=details.pk).update(
                reminder_due_at=timezone.now() - datetime.timedelta(minutes=1)
            )
            send_appointment_reminders()
            send_appointment_reminders()
        send.assert_called_once()
        self.assertIn(details.appointment_number, send.call_args.kwargs["content"])
        self.assertEqual(send.call_args.kwargs["recipients"], "+919812345678")

    def test_walk_in_only_for_today(self):
        response = self.book(booking_channel="walk_in")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only be made for today", str(response.data))

        today_slot = self.create_slot(timezone.now() + datetime.timedelta(minutes=30))
        response = self.book(slot=today_slot, booking_channel="walk_in")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_walk_in_reserve_holds_seats_back(self):
        response = self.client.put(
            reverse("walk-in-reserve"),
            {"availability": str(self.availability.external_id), "walk_in_every": 4},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patients = [
            self.create_patient(phone_number=f"+91981234560{i}") for i in range(4)
        ]
        for slot, patient in zip(self.slots[:3], patients, strict=False):
            self.assertEqual(self.book(slot=slot, patient=patient).status_code, 200)
        # 4 seats, 1 held for walk-ins: the 4th scheduled booking is refused.
        response = self.book(slot=self.slots[3], patient=patients[3])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("held for walk-ins", str(response.data))

        response = self.client.put(
            reverse("walk-in-reserve"),
            {"availability": str(self.availability.external_id), "walk_in_every": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(WalkInReserve.objects.exists())
        response = self.book(slot=self.slots[3], patient=patients[3])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_check_in_takes_class_from_consultation_type(self):
        booking_id = self.book(consultation_type="video").data["booking"]["id"]
        body = {
            "patient": str(self.patient.external_id),
            "facility": str(self.facility.external_id),
            "organizations": [str(self.organization.external_id)],
            "status": "in_progress",
            "encounter_class": "amb",
            "priority": "routine",
            "appointment": booking_id,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("encounter-list"), body, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["encounter_class"], "vr")
        self.assertEqual(
            Encounter.objects.get(external_id=response.data["id"]).encounter_class, "vr"
        )

    def test_shared_desk_number_does_not_receive_patient_reminders(self):
        EmergencyDesk.objects.create(
            facility=self.facility, phone_number=self.patient.phone_number
        )
        response = self.book(booking_channel="phone")
        self.assertEqual(response.status_code, 200, response.data)
        AppointmentDetails.objects.filter(
            external_id=response.data["details"]["id"]
        ).update(reminder_due_at=timezone.now() - datetime.timedelta(minutes=1))
        with mock.patch("care_emergency.tasks.send_text_message") as send:
            send_appointment_reminders()
            send.assert_not_called()
