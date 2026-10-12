from care.utils.sms import send_text_message
from care.utils.time_util import care_now
from celery import Celery, current_app, shared_task
from django.utils import timezone

from care_emergency.models import AppointmentDetails, PatientRegistration
from care_emergency.contact import is_desk_phone


@shared_task
def send_appointment_reminders():
    """Texts phone-booked patients whose reminder is due and whose booking is
    still open. Each reminder is claimed before sending, so a second worker
    run never sends it twice."""
    due = AppointmentDetails.objects.filter(
        reminder_due_at__lte=care_now(),
        reminder_sent_at__isnull=True,
        booking__status="booked",
        booking__token_slot__start_datetime__gt=care_now(),
    ).select_related("booking__patient", "booking__token_slot__resource", "facility")
    for details in due:
        claimed = AppointmentDetails.objects.filter(
            pk=details.pk, reminder_sent_at__isnull=True
        ).update(reminder_sent_at=care_now())
        if not claimed:
            continue
        booking = details.booking
        if (
            is_desk_phone(booking.patient.phone_number)
            or PatientRegistration.objects.filter(
                patient=booking.patient, mobile_owner="No phone"
            ).exists()
        ):
            continue
        start = timezone.localtime(booking.token_slot.start_datetime)
        doctor = booking.token_slot.resource.user
        with_doctor = f" with {doctor.get_full_name()}" if doctor else ""
        send_text_message(
            content=(
                f"Reminder: appointment {details.appointment_number}{with_doctor} "
                f"at {details.facility.name}, {start:%d %b %Y %I:%M %p}."
            ),
            recipients=booking.patient.phone_number,
            fail_silently=True,
        )


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender: Celery, **kwargs):
    sender.add_periodic_task(
        5 * 60, send_appointment_reminders.s(), name="send_appointment_reminders"
    )
