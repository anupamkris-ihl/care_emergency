import datetime
from unittest import mock

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from care_emergency.models import FacilityUnit, PostOffice, QuickRegistration

PHONE = "+919812345678"


class QuickRegistrationTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()  # the public form's throttle counts requests in the cache
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.department = self.create_facility_organization(
            facility=self.facility, org_type="dept", name="Cardiology"
        )
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()

    def body(self, **kwargs):
        return {
            "facility": str(self.facility.external_id),
            "title": "Ms",
            "first_name": "Neha",
            "last_name": "Sharma",
            "date_of_birth": "1999-04-12",
            "gender": "female",
            "phone_number": PHONE,
            "consultation_type": "in_person",
            "department": str(self.department.external_id),
            "send_form_link": True,
            **kwargs,
        }

    def register(self, body):
        with (
            mock.patch("care_emergency.views.send_text_message", return_value=1) as sms,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                reverse("quick-registration"), body, format="json"
            )
        return response, sms

    def form_url(self, record):
        return reverse("quick-registration-form", args=[record.form_token])

    def form_answers(self, **kwargs):
        return {
            "email": "neha@example.com",
            "house": "C-22",
            "street": "Ring Road",
            "pincode": "110024",
            "preferred_language": "Hindi",
            "next_of_kin_name": "Ravi Sharma",
            "next_of_kin_relationship": "Brother",
            **kwargs,
        }

    def test_registers_patient_with_pre_reference_and_texts_link(self):
        response, sms = self.register(self.body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.assertEqual(patient.name, "Neha Sharma")
        record = QuickRegistration.objects.get(patient=patient)
        self.assertEqual(record.title, "Ms")
        self.assertEqual(record.department, self.department)
        self.assertTrue(record.send_payment_reminder)
        self.assertIsNotNone(record.form_link_sent_at)
        sms.assert_called_once()
        self.assertEqual(sms.call_args.kwargs["recipients"], PHONE)
        self.assertIn(
            f"/register/complete/{record.form_token}", sms.call_args.kwargs["content"]
        )
        numbers = [x["value"] for x in response.data["patient"]["instance_identifiers"]]
        self.assertEqual(numbers, ["PRE-LN-0000001"])

        detail = self.client.get(
            reverse("quick-registration-detail", args=[patient.external_id])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["department"]["name"], "Cardiology")
        self.assertNotIn("form_token", detail.data)

    def test_no_link_when_not_asked(self):
        response, sms = self.register(
            self.body(
                send_form_link=False, department=None, doctor=str(self.user.external_id)
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        sms.assert_not_called()
        record = QuickRegistration.objects.get()
        self.assertEqual(record.doctor, self.user)
        self.assertIsNone(record.form_link_sent_at)

    def test_rejects_bad_requests_without_creating_a_patient(self):
        cases = [
            (self.body(title="Mr"), "title does not match"),
            (self.body(department=None), "department or a doctor"),
            (self.body(first_name="N"), "first name"),
            (
                self.body(
                    date_of_birth=(
                        timezone.localdate() + datetime.timedelta(days=1)
                    ).isoformat()
                ),
                "between 1900 and today",
            ),
            (
                self.body(
                    department=str(
                        self.create_facility_organization(
                            facility=self.create_facility(user=self.user),
                            org_type="dept",
                        ).external_id
                    )
                ),
                "",
            ),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                response, sms = self.register(body)
                self.assertIn(response.status_code, (400, 404), response.data)
                self.assertIn(message, str(response.data))
                sms.assert_not_called()
        self.assertFalse(QuickRegistration.objects.exists())

    def test_patient_completes_form_once_without_login(self):
        self.register(self.body())
        record = QuickRegistration.objects.get()
        self.client.force_authenticate(user=None)

        opened = self.client.get(self.form_url(record))
        self.assertEqual(opened.status_code, status.HTTP_200_OK)
        self.assertEqual(opened.data["status"], "open")
        self.assertEqual(opened.data["first_name"], "Neha")
        self.assertEqual(opened.data["phone_last4"], "5678")
        self.assertNotIn("last_name", opened.data)

        submitted = self.client.post(
            self.form_url(record), self.form_answers(), format="json"
        )
        self.assertEqual(submitted.status_code, status.HTTP_200_OK, submitted.data)
        record.refresh_from_db()
        self.assertIsNotNone(record.form_submitted_at)
        self.assertEqual(record.form_details["house"], "C-22")
        self.assertEqual(record.form_details["whatsapp_number"], PHONE)
        self.assertEqual(
            self.client.get(self.form_url(record)).data["status"], "submitted"
        )

        again = self.client.post(
            self.form_url(record), self.form_answers(), format="json"
        )
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been submitted", str(again.data))

    def test_form_rejects_bad_answers_and_expired_links(self):
        self.register(self.body())
        record = QuickRegistration.objects.get()
        self.client.force_authenticate(user=None)

        bad = self.client.post(
            self.form_url(record),
            self.form_answers(pincode="012345", preferred_language="Klingon"),
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        record.refresh_from_db()
        self.assertIsNone(record.form_submitted_at)

        PostOffice.objects.create(
            pincode="110024",
            name="Lajpat Nagar",
            office_type="PO",
            delivery=True,
            district="South Delhi",
            state="Delhi",
            loaded_at=timezone.now(),
        )
        pin = self.client.get(
            reverse(
                "quick-registration-form-pincode", args=[record.form_token, "110024"]
            )
        )
        self.assertEqual(pin.status_code, status.HTTP_200_OK, pin.data)
        self.assertEqual(pin.data["district"], "South Delhi")
        wrong_post_office = self.client.post(
            self.form_url(record),
            self.form_answers(locality="Saket", district="South Delhi", state="Delhi"),
            format="json",
        )
        self.assertIn("post office", str(wrong_post_office.data))

        missing = self.client.get(
            reverse("quick-registration-form", args=["not-a-real-token"])
        )
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

        with freeze_time(timezone.now() + datetime.timedelta(days=8)):
            self.assertEqual(
                self.client.get(self.form_url(record)).data["status"], "expired"
            )
            late = self.client.post(
                self.form_url(record),
                self.form_answers(
                    locality="Lajpat Nagar", district="South Delhi", state="Delhi"
                ),
                format="json",
            )
            closed_pin = self.client.get(
                reverse(
                    "quick-registration-form-pincode",
                    args=[record.form_token, "110024"],
                )
            )
        self.assertEqual(closed_pin.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(late.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("expired", str(late.data))

    def test_desk_detail_needs_a_login(self):
        response, _ = self.register(self.body())
        self.client.force_authenticate(user=None)
        detail = self.client.get(
            reverse("quick-registration-detail", args=[response.data["patient"]["id"]])
        )
        self.assertIn(detail.status_code, (401, 403))

    def test_desk_resends_link_and_reopens_it(self):
        self.register(self.body(send_form_link=False))
        record = QuickRegistration.objects.get()
        url = reverse("quick-registration-send-link", args=[record.patient.external_id])

        with mock.patch("care_emergency.views.send_text_message", return_value=0):
            failed = self.client.post(url)
        self.assertEqual(failed.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("couldn't be sent", str(failed.data))

        with freeze_time(timezone.now() + datetime.timedelta(days=8)):
            with mock.patch(
                "care_emergency.views.send_text_message", return_value=1
            ) as sms:
                resent = self.client.post(url)
            self.assertEqual(resent.status_code, status.HTTP_200_OK, resent.data)
            sms.assert_called_once()
            self.assertTrue(resent.data["send_form_link"])
            self.assertIsNotNone(resent.data["form_link_sent_at"])
            self.client.force_authenticate(user=None)
            self.assertEqual(
                self.client.get(self.form_url(record)).data["status"], "open"
            )
