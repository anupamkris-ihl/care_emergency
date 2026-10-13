"""Billing rules Care's own billing APIs don't enforce (front-desk v11
billing.md §2, §5, §8, §9; Scope R35, R36, R48, VAL-15, VAL-16, D57, D58, INV,
RCT, L16). They run as pre_save checks on Care's charge items, invoices and
payments, so a broken rule is refused with a 400 from Care's own endpoints.

They apply only in facilities with a unit code, and the charge item rules only
to lines from the POH catalogue (setup_billing): pharmacy and other Care
charge items are left alone."""

from decimal import Decimal

from care.emr.extensions.base import ExtensionResource, PlugExtension
from care.emr.models import Encounter
from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.charge_item.spec import CHARGE_ITEM_CANCELLED_STATUS
from care.emr.resources.charge_item.sync_charge_item_costs import (
    sync_charge_item_costs,
)
from care.security.authorization import AuthorizationController
from jsonschema import ValidationError as SchemaError
from jsonschema import validate
from rest_framework.exceptions import ValidationError

from care_emergency.billing import (
    DISCOUNT_SYSTEM,
    HEADS,
    PICKED_DISCOUNTS,
    slug_for,
)
from care_emergency.constants import PatientNumberSeries, VisitPurposes
from care_emergency.models import AppointmentDetails, FacilityUnit
from care_emergency.services import (
    allocate_document_number,
    default_visit_purpose,
    patient_number,
)

SAC_SYSTEM = "https://www.cbic.gov.in/gst/sac"
# Consultations that are free inside the doctor's follow-up window (R36).
CONSULTATIONS = {"CON2", "VID", "HOMEV"}
REGISTRATION = "REG"
MAX_QUANTITY = 20
# Whole rupees, at most 7 digits (VAL-16).
MAX_AMOUNT = Decimal(10_000_000)
# A patient-caused refund above this needs a supervisor (billing.md §9).
REFUND_AUTO_APPROVE_LIMIT = Decimal(500)

ADVANCE_PURPOSES = [
    "OPD procedure",
    "IP admission",
    "Day-care",
    "IVF package",
    "Health check package",
    "Home care",
]
REFUND_CAUSES = ["hospital", "patient"]
REFUND_REASONS = [
    "Service not rendered",
    "Wrong service billed",
    "Wrong patient / duplicate bill",
    "Patient declined after payment",
    "Doctor unavailable",
    "Machine down",
    "Rate corrected",
]

# What the desk sends with a payment, under extensions.billing. number is set
# by the server: a value sent back is ignored.
BILLING_EXTENSION_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"enum": ADVANCE_PURPOSES},
        "cause": {"enum": REFUND_CAUSES},
        "reason": {"enum": REFUND_REASONS},
        "number": {"type": "string"},
    },
    "additionalProperties": False,
}


class BillingExtension(PlugExtension):
    """extensions.billing on a payment: advance purpose, refund cause and
    reason, and the server-issued RCT / ADV / RFD number."""

    resource_type = ExtensionResource.payment_reconciliation
    extension_name = "billing"
    extension_version = "1"
    write_schema = BILLING_EXTENSION_SCHEMA
    read_schema = BILLING_EXTENSION_SCHEMA
    retrieve_schema = BILLING_EXTENSION_SCHEMA

    def validate(self, data, resource=None):
        try:
            validate(data, BILLING_EXTENSION_SCHEMA)
        except SchemaError as e:
            raise ValueError(e.message) from e
        return data


def has_unit(facility_id):
    return FacilityUnit.objects.filter(facility_id=facility_id).exists()


def catalogue_code(item):
    """The POH catalogue code (REG, CON2, …) of a charge item, or None."""
    definition = item.charge_item_definition
    if definition is None or not definition.purpose:
        return None
    if not definition.slug.endswith(slug_for(definition.purpose)):
        return None
    return definition.purpose


def item_head(item):
    category = item.charge_item_definition.category
    if category is None:
        return None
    for head in HEADS:
        if category.slug.endswith(slug_for(head)):
            return head
    return None


def entitlement(code):
    """The 100% entitlement component from the facility discount list."""
    display = next(display for key, display, *_ in PICKED_DISCOUNTS if key == code)
    return {
        "monetary_component_type": "discount",
        "code": {"system": DISCOUNT_SYSTEM, "code": code, "display": display},
        "factor": "100",
    }


def grant_entitlement(item, code):
    """Zero the line with an entitlement. It replaces any concession, which
    only applies when there is no entitlement (billing.md §2)."""
    item.unit_price_components = [
        component
        for component in item.unit_price_components or []
        if component.get("monetary_component_type") != "discount"
    ] + [entitlement(code)]
    sync_charge_item_costs(item)


def registration_already_charged(item):
    """R35: the registration fee is charged once in a lifetime, at any POH
    facility."""
    return (
        ChargeItem.objects.filter(
            patient_id=item.patient_id,
            charge_item_definition__purpose=REGISTRATION,
            charge_item_definition__slug__endswith=slug_for(REGISTRATION),
        )
        .exclude(status__in=CHARGE_ITEM_CANCELLED_STATUS)
        .exists()
    )


def follow_up_inside_window(item):
    """R36: the encounter's booking is a follow-up with the same doctor inside
    the schedule's revisit window."""
    if not item.encounter_id:
        return False
    booking = (
        Encounter.objects.filter(id=item.encounter_id)
        .values_list("appointment_id", flat=True)
        .first()
    )
    if not booking:
        return False
    details = (
        AppointmentDetails.objects.filter(booking_id=booking)
        .select_related("booking__token_slot__availability__schedule")
        .first()
    )
    if details is None or details.visit_purpose != VisitPurposes.follow_up:
        return False
    return (
        default_visit_purpose(details.booking.token_slot, item.patient)
        == VisitPurposes.follow_up
    )


def check_quantity(item, head):
    """R48 / VAL-15: a whole number from 1 to 20. Ward days accrue per day of
    stay and aren't capped."""
    if head == "WARD" or item.quantity is None:
        return
    quantity = Decimal(item.quantity)
    if quantity != quantity.to_integral_value() or not (1 <= quantity <= MAX_QUANTITY):
        raise ValidationError(
            {"quantity": f"Quantity must be a whole number from 1 to {MAX_QUANTITY}"}
        )


def check_charge_item(item):
    code = catalogue_code(item)
    if code is None or not has_unit(item.facility_id):
        return
    head = item_head(item)
    if item._state.adding:
        check_quantity(item, head)
    else:
        previous = (
            ChargeItem.objects.filter(id=item.id)
            .values_list("quantity", flat=True)
            .first()
        )
        if previous != item.quantity:
            check_quantity(item, head)
        return
    if code == REGISTRATION and registration_already_charged(item):
        grant_entitlement(item, "entitlement_registration")
    elif code in CONSULTATIONS and follow_up_inside_window(item):
        grant_entitlement(item, "entitlement_follow_up")
    # L16: the SAC of the line's head, copied onto the invoice line.
    if not item.code and head:
        sac = HEADS[head][1]
        item.code = {"system": SAC_SYSTEM, "code": sac, "display": f"SAC {sac}"}


def highest_invoice_number(facility_id):
    def seed(prefix):
        numbers = Invoice.objects.filter(
            facility_id=facility_id, number__startswith=prefix
        ).values_list("number", flat=True)
        return max(
            (int(n[len(prefix) :]) for n in numbers if n[len(prefix) :].isdigit()),
            default=0,
        )

    return seed


def number_invoice(invoice):
    """INV numbers (CN for item-return invoices) restart every year, under a
    row lock. Care's own expression counts invoices since the beginning."""
    if not invoice._state.adding or not has_unit(invoice.facility_id):
        return
    series = "CN" if invoice.is_refund else "INV"
    invoice.number = allocate_document_number(
        series, invoice.facility, seed=highest_invoice_number(invoice.facility_id)
    )


def check_amounts(payment):
    for field in ("tendered_amount", "returned_amount", "amount"):
        value = getattr(payment, field)
        if value is None:
            continue
        value = Decimal(value)
        if value != value.to_integral_value() or not (0 <= value < MAX_AMOUNT):
            raise ValidationError({field: "Amounts are whole rupees, up to 7 digits"})


def is_supervisor(user, facility):
    return user is not None and AuthorizationController.call(
        "can_destroy_payment_reconciliation_in_facility", user, facility
    )


def is_unidentified(patient):
    """An unknown patient keeps only their UNK number until registered."""
    return bool(patient_number(patient, PatientNumberSeries.unknown)) and not (
        patient_number(patient, PatientNumberSeries.uhid)
    )


def payment_series(payment, billing):
    if payment.is_credit_note:
        if billing.get("cause") not in REFUND_CAUSES:
            raise ValidationError({"extensions": "A refund needs its cause"})
        if billing.get("reason") not in REFUND_REASONS:
            raise ValidationError({"extensions": "A refund needs a reason"})
        if (
            billing["cause"] == "patient"
            and Decimal(payment.amount) > REFUND_AUTO_APPROVE_LIMIT
            and not is_supervisor(payment.created_by, payment.facility)
        ):
            raise ValidationError(
                "A patient-caused refund above ₹500 needs a supervisor"
            )
        return "RFD"
    if payment.reconciliation_type == "advance":
        if is_unidentified(payment.account.patient):
            raise ValidationError("Complete the patient's registration first")
        if billing.get("purpose") not in ADVANCE_PURPOSES:
            raise ValidationError({"extensions": "An advance needs its purpose"})
        return "ADV"
    if payment.reconciliation_type == "payment":
        return "RCT"
    return None


def check_payment(payment):
    """VAL-16 amounts, D57 advance purpose, D58 refund cause, reason and
    approval, and the RCT / ADV / RFD number (kept on later updates)."""
    if not has_unit(payment.facility_id):
        return
    extensions = dict(payment.extensions or {})
    billing = dict(extensions.get("billing") or {})
    if not payment._state.adding:
        previous = (
            PaymentReconciliation.objects.filter(id=payment.id)
            .values_list("extensions", flat=True)
            .first()
        ) or {}
        number = (previous.get("billing") or {}).get("number")
        if number:
            billing["number"] = number
            extensions["billing"] = billing
            payment.extensions = extensions
        return
    check_amounts(payment)
    billing.pop("number", None)
    series = payment_series(payment, billing)
    if series:
        billing["number"] = allocate_document_number(series, payment.facility)
    if billing:
        extensions["billing"] = billing
    payment.extensions = extensions
