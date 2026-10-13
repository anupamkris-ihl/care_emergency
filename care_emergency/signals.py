from care.emr.models import Encounter
from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from care_emergency.constants import CONSULTATION_ENCOUNTER_CLASS, PatientNumberSeries
from care_emergency.billing import sync_billing_tags
from care_emergency.billing_rules import (
    check_charge_item,
    check_payment,
    number_invoice,
)
from care_emergency.models import AppointmentDetails, FacilityUnit, PatientRegistration
from care_emergency.services import issue_patient_number, patient_number


@receiver(pre_save, sender=Encounter, dispatch_uid="care_emergency_checkin_class")
def copy_consultation_type(sender, instance, **kwargs):
    """At check-in, an outpatient encounter created from a booking takes its
    class from the booking's consultation type (video → vr, home visit → hh).
    Any other class the desk chose, such as an admission, is kept."""
    if not instance._state.adding or not instance.appointment_id:
        return
    if instance.encounter_class != "amb":
        return
    details = AppointmentDetails.objects.filter(
        booking_id=instance.appointment_id
    ).first()
    if details:
        instance.encounter_class = CONSULTATION_ENCOUNTER_CLASS[
            details.consultation_type
        ]


@receiver(post_save, sender=Encounter, dispatch_uid="care_emergency_issue_uhid")
def issue_uhid_on_arrival(sender, instance, created, **kwargs):
    """A patient without a UHID gets one at their first encounter in a unit
    with a code: pre-registrations convert on arrival, and patients registered
    outside the desk are covered too. Unknown patients keep their UNK number
    until registration is completed (ConvertView)."""
    if not created:
        return
    if not FacilityUnit.objects.filter(facility_id=instance.facility_id).exists():
        return
    patient = instance.patient
    if patient_number(patient, PatientNumberSeries.unknown):
        return
    issue_patient_number(patient, instance.facility)


@receiver(
    post_save, sender=PatientRegistration, dispatch_uid="care_emergency_billing_tags"
)
def tag_payer_for_billing(sender, instance, **kwargs):
    """Keeps the patient's payer and concession tags in step with the desk
    registration, so Care prices their charge items at the right tariff."""
    sync_billing_tags(instance)


@receiver(pre_save, sender=ChargeItem, dispatch_uid="care_emergency_charge_rules")
def apply_charge_rules(sender, instance, **kwargs):
    """Quantity, once-in-a-lifetime registration, free follow-up and SAC on
    POH catalogue lines (billing_rules)."""
    check_charge_item(instance)


@receiver(pre_save, sender=Invoice, dispatch_uid="care_emergency_invoice_number")
def apply_invoice_number(sender, instance, **kwargs):
    number_invoice(instance)


@receiver(
    pre_save, sender=PaymentReconciliation, dispatch_uid="care_emergency_payment_rules"
)
def apply_payment_rules(sender, instance, **kwargs):
    """Whole-rupee amounts, advance purpose, refund cause and approval, and the
    receipt number (billing_rules)."""
    check_payment(instance)
