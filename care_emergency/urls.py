from django.urls import path
from rest_framework.routers import SimpleRouter

from care_emergency.views import (
    AppointmentViewSet,
    ConvertView,
    DeskView,
    EmergencyEncounterViewSet,
    PatientNumberView,
    PatientRegistrationView,
    RegisterView,
    RegistrationView,
    UnitView,
    WalkInReserveView,
)

router = SimpleRouter()
router.register("encounters", EmergencyEncounterViewSet, basename="emergency-encounter")
router.register("appointments", AppointmentViewSet, basename="desk-appointment")

urlpatterns = [
    path("desk/", DeskView.as_view(), name="emergency-desk"),
    path("unit/", UnitView.as_view(), name="facility-unit"),
    path(
        "patients/<uuid:patient_id>/uhid/",
        PatientNumberView.as_view(),
        name="patient-uhid",
    ),
    path("register/", RegisterView.as_view(), name="emergency-register"),
    path(
        "patients/<uuid:patient_id>/convert/",
        ConvertView.as_view(),
        name="emergency-convert",
    ),
    path("registration/", RegistrationView.as_view(), name="desk-registration"),
    path(
        "registration/<uuid:patient_id>/",
        PatientRegistrationView.as_view(),
        name="desk-registration-detail",
    ),
    path("walk-in-reserve/", WalkInReserveView.as_view(), name="walk-in-reserve"),
    *router.urls,
]
