import datetime
from decimal import Decimal

from care.emr.models import (
    Availability,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.emr.models.charge_item import ChargeItem
from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.account.default_account import get_default_account
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from django.utils import timezone
from rest_framework import status

from care_emergency.billing import setup_billing, slug_for
from care_emergency.constants import PatientNumberSeries
from care_emergency.models import AppointmentDetails, FacilityUnit
from care_emergency.services import attach_patient_number


class BillingRuleTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        setup_billing(self.facility)
        self.patient = self.create_patient()
        self.account = get_default_account(self.patient, self.facility)
        self.client.force_authenticate(user=self.user)
        self.base = f"/api/v1/facility/{self.facility.external_id}"
        self.yy = care_now().year % 100

    def slug(self, code):
        return ChargeItemDefinition.calculate_slug_from_facility(
            self.facility.external_id, slug_for(code)
        )

    def charge(self, code, quantity=1, encounter=None, patient=None):
        request = {"charge_item_definition": self.slug(code), "quantity": quantity}
        if encounter:
            request["encounter"] = str(encounter.external_id)
        else:
            request["patient"] = str((patient or self.patient).external_id)
        return self.client.post(
            f"{self.base}/charge_item/apply_charge_item_defs/",
            {"requests": [request]},
            format="json",
        )

    def last_item(self, code):
        return (
            ChargeItem.objects.filter(charge_item_definition__purpose=code)
            .order_by("-id")
            .first()
        )

    def pay(self, tendered, returned=0, **kwargs):
        body = {
            "reconciliation_type": "payment",
            "status": "active",
            "kind": "deposit",
            "issuer_type": "patient",
            "outcome": "complete",
            "method": "cash",
            "account": str(self.account.external_id),
            "tendered_amount": str(tendered),
            "returned_amount": str(returned),
            **kwargs,
        }
        return self.client.post(
            f"{self.base}/payment_reconciliation/", body, format="json"
        )

    def invoice(self, *items):
        return self.client.post(
            f"{self.base}/invoice/",
            {
                "account": str(self.account.external_id),
                "status": "draft",
                "charge_items": [str(item.external_id) for item in items],
            },
            format="json",
        )

    # R48, VAL-15
    def test_quantity_is_a_whole_number_from_1_to_20(self):
        self.assertEqual(self.charge("CBC", 20).status_code, status.HTTP_200_OK)
        response = self.charge("CBC", 21)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("1 to 20", str(response.data))
        # Ward days accrue per day of stay.
        self.assertEqual(self.charge("WARD-GEN", 30).status_code, status.HTTP_200_OK)
        item = self.last_item("CBC")
        update = self.client.put(
            f"{self.base}/charge_item/{item.external_id}/",
            {
                "title": item.title,
                "status": "billable",
                "quantity": "2.5",
                "unit_price_components": item.unit_price_components,
            },
            format="json",
        )
        # Catalogue lines can't be repriced, so Care keeps the old quantity.
        self.assertEqual(update.status_code, status.HTTP_200_OK, update.data)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 20)

    # R35
    def test_registration_fee_once_in_a_lifetime(self):
        self.charge("REG")
        first = self.last_item("REG")
        self.assertEqual(first.total_price, Decimal(200))
        self.charge("REG")
        second = self.last_item("REG")
        self.assertEqual(second.total_price, 0)
        self.assertEqual(
            second.total_price_components[-1]["code"]["code"],
            "entitlement_registration",
        )
        # A concession never stacks on an entitlement.
        self.assertEqual(
            [c["monetary_component_type"] for c in second.total_price_components],
            ["base", "discount"],
        )
        # Another POH facility counts too.
        other = self.create_facility(user=self.user)
        FacilityUnit.objects.create(facility=other, code="KB")
        setup_billing(other)
        self.client.post(
            f"/api/v1/facility/{other.external_id}/charge_item/apply_charge_item_defs/",
            {
                "requests": [
                    {
                        "charge_item_definition": ChargeItemDefinition.calculate_slug_from_facility(
                            other.external_id, slug_for("REG")
                        ),
                        "quantity": 1,
                        "patient": str(self.patient.external_id),
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(self.last_item("REG").total_price, 0)

    def test_cancelled_registration_fee_is_charged_again(self):
        self.charge("REG")
        ChargeItem.objects.filter(id=self.last_item("REG").id).update(
            status="entered_in_error"
        )
        self.charge("REG")
        self.assertEqual(self.last_item("REG").total_price, Decimal(200))

    # R36
    def test_follow_up_inside_the_window_is_free(self):
        resource = SchedulableResource.objects.create(
            facility=self.facility, resource_type="practitioner", user=self.user
        )
        schedule = Schedule.objects.create(
            resource=resource,
            name="OPD",
            valid_from=timezone.now() - datetime.timedelta(days=30),
            valid_to=timezone.now() + datetime.timedelta(days=30),
            revisit_allowed_days=7,
        )
        availability = Availability.objects.create(
            schedule=schedule,
            name="Morning",
            slot_type="appointment",
            slot_size_in_minutes=15,
            tokens_per_slot=1,
            availability=[],
        )

        def visit(days_ago, purpose, number):
            start = timezone.now() - datetime.timedelta(days=days_ago)
            slot = TokenSlot.objects.create(
                resource=resource,
                availability=availability,
                start_datetime=start,
                end_datetime=start + datetime.timedelta(minutes=15),
            )
            booking = TokenBooking.objects.create(
                token_slot=slot,
                patient=self.patient,
                booked_by=self.user,
                status="fulfilled",
            )
            AppointmentDetails.objects.create(
                booking=booking,
                facility=self.facility,
                appointment_number=number,
                consultation_type="in_person",
                visit_purpose=purpose,
                booking_channel="front_desk",
            )
            return self.create_encounter(
                self.patient, self.facility, self.organization, appointment=booking
            )

        first = visit(5, "new", "APT-T-1")
        self.charge("CON2", encounter=first)
        self.assertEqual(self.last_item("CON2").total_price, Decimal(900))
        follow_up = visit(0, "follow_up", "APT-T-2")
        self.charge("CON2", encounter=follow_up)
        item = self.last_item("CON2")
        self.assertEqual(item.total_price, 0)
        self.assertEqual(
            item.total_price_components[-1]["code"]["code"], "entitlement_follow_up"
        )
        # Marked follow-up but outside the 7-day window: charged.
        late = visit(-10, "follow_up", "APT-T-3")
        self.charge("CON2", encounter=late)
        self.assertEqual(self.last_item("CON2").total_price, Decimal(900))

    # L16
    def test_lines_carry_their_sac(self):
        self.charge("CON2")
        self.charge("CBC")
        self.assertEqual(self.last_item("CON2").code["code"], "999312")
        self.assertEqual(self.last_item("CBC").code["code"], "999316")
        response = self.invoice(self.last_item("CBC"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        invoice = Invoice.objects.get(external_id=response.data["id"])
        self.assertEqual(invoice.charge_items_copy[0]["code"]["code"], "999316")

    # INV
    def test_invoice_numbers_count_per_year(self):
        # Invoices numbered by Care's all-time expression before this rule
        # (bulk_create skips the numbering signal).
        Invoice.objects.bulk_create(
            Invoice(
                facility=self.facility,
                patient=self.patient,
                account=self.account,
                status="draft",
                number=number,
            )
            for number in (f"INV/LN/{self.yy - 1}/00041", f"INV/LN/{self.yy}/00007")
        )
        self.charge("CBC")
        response = self.invoice(self.last_item("CBC"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The year's counter starts after the highest number already issued.
        self.assertEqual(response.data["number"], f"INV/LN/{self.yy}/00008")
        self.charge("LFT")
        response = self.invoice(self.last_item("LFT"))
        self.assertEqual(response.data["number"], f"INV/LN/{self.yy}/00009")

    # R49
    def test_issued_invoice_is_never_edited(self):
        self.charge("CBC")
        item = self.last_item("CBC")
        invoice_id = self.invoice(item).data["id"]
        issued = self.client.put(
            f"{self.base}/invoice/{invoice_id}/",
            {"account": str(self.account.external_id), "status": "issued"},
            format="json",
        )
        self.assertEqual(issued.status_code, status.HTTP_200_OK, issued.data)
        self.charge("LFT")
        attach = self.client.post(
            f"{self.base}/invoice/{invoice_id}/attach_items_to_invoice/",
            {"charge_items": [str(self.last_item("LFT").external_id)]},
            format="json",
        )
        self.assertEqual(attach.status_code, status.HTTP_400_BAD_REQUEST)
        edit = self.client.put(
            f"{self.base}/charge_item/{item.external_id}/",
            {
                "title": "Changed",
                "status": "billed",
                "quantity": "1",
                "unit_price_components": item.unit_price_components,
            },
            format="json",
        )
        self.assertEqual(edit.status_code, status.HTTP_400_BAD_REQUEST)
        back = self.client.put(
            f"{self.base}/invoice/{invoice_id}/",
            {"account": str(self.account.external_id), "status": "draft"},
            format="json",
        )
        self.assertEqual(back.status_code, status.HTTP_400_BAD_REQUEST)

    # VAL-16, R47, RCT
    def test_payment_amounts_mode_and_receipt_number(self):
        response = self.pay("500.50")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("whole rupees", str(response.data))
        self.assertEqual(self.pay("10000000").status_code, status.HTTP_400_BAD_REQUEST)
        paid = self.pay(500)
        self.assertEqual(paid.status_code, status.HTTP_200_OK, paid.data)
        body = {
            "reconciliation_type": "payment",
            "status": "active",
            "kind": "deposit",
            "issuer_type": "patient",
            "outcome": "complete",
            "account": str(self.account.external_id),
            "tendered_amount": "500",
            "returned_amount": "0",
        }
        missing = self.client.post(
            f"{self.base}/payment_reconciliation/", body, format="json"
        )
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("method", str(missing.data))

        payment = PaymentReconciliation.objects.get(external_id=paid.data["id"])
        self.assertEqual(
            payment.extensions["billing"]["number"], f"RCT/LN/{self.yy}/00001"
        )
        self.assertEqual(
            self.pay(1000).data["extensions"]["billing"]["number"],
            f"RCT/LN/{self.yy}/00002",
        )
        # The number survives an update that leaves it out.
        update = self.client.put(
            f"{self.base}/payment_reconciliation/{payment.external_id}/",
            {**body, "method": "cash", "note": "Counter A-02", "extensions": {}},
            format="json",
        )
        self.assertEqual(update.status_code, status.HTTP_200_OK, update.data)
        payment.refresh_from_db()
        self.assertEqual(payment.note, "Counter A-02")
        self.assertEqual(
            payment.extensions["billing"]["number"], f"RCT/LN/{self.yy}/00001"
        )

    # D57
    def test_advance_needs_a_purpose(self):
        response = self.pay(5000, reconciliation_type="advance")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("purpose", str(response.data))
        response = self.pay(
            5000,
            reconciliation_type="advance",
            extensions={"billing": {"purpose": "Teeth whitening"}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.pay(
            5000,
            reconciliation_type="advance",
            extensions={"billing": {"purpose": "IP admission", "number": "ADV/X"}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["extensions"]["billing"],
            {"purpose": "IP admission", "number": f"ADV/LN/{self.yy}/00001"},
        )
        # Not for an unknown patient until their registration is complete.
        attach_patient_number(
            self.patient, PatientNumberSeries.unknown, "UNK-LN-20261013-001"
        )
        response = self.pay(
            5000,
            reconciliation_type="advance",
            extensions={"billing": {"purpose": "IP admission"}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("registration", str(response.data))

    # D58
    def test_refund_cause_reason_and_approval(self):
        refund = {"is_credit_note": True}
        response = self.pay(300, **refund)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cause", str(response.data))
        response = self.pay(
            300, **refund, extensions={"billing": {"cause": "hospital"}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", str(response.data))

        cashier = self.create_user()
        self.attach_role_facility_organization_user(
            self.organization,
            cashier,
            self.create_role_with_permissions(
                ["can_write_payment_reconciliation", "can_read_payment_reconciliation"],
                role_name="Cashier",
            ),
        )
        self.client.force_authenticate(user=cashier)
        patient_caused = {
            "billing": {"cause": "patient", "reason": "Patient declined after payment"}
        }
        response = self.pay(600, **refund, extensions=patient_caused)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("supervisor", str(response.data))
        # Up to ₹500, or hospital-caused, is approved automatically.
        response = self.pay(500, **refund, extensions=patient_caused)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["extensions"]["billing"]["number"],
            f"RFD/LN/{self.yy}/00001",
        )
        hospital_caused = {
            "billing": {"cause": "hospital", "reason": "Doctor unavailable"}
        }
        response = self.pay(600, **refund, extensions=hospital_caused)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(user=self.user)
        response = self.pay(600, **refund, extensions=patient_caused)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_facilities_without_a_unit_are_left_alone(self):
        other = self.create_facility(user=self.user)
        account = get_default_account(self.patient, other)
        response = self.client.post(
            f"/api/v1/facility/{other.external_id}/payment_reconciliation/",
            {
                "reconciliation_type": "payment",
                "status": "active",
                "kind": "deposit",
                "issuer_type": "patient",
                "outcome": "complete",
                "method": "cash",
                "account": str(account.external_id),
                "tendered_amount": "10.50",
                "returned_amount": "0",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("billing", response.data.get("extensions") or {})
