from django.urls import path
from rest_framework.routers import SimpleRouter

from care_emergency.views import (
    AppointmentViewSet,
    BreakGlassView,
    ConvertView,
    DeskView,
    DocumentView,
    EmergencyEncounterViewSet,
    PatientNumberView,
    PatientNumberSearchView,
    PatientContactView,
    PatientRegistrationView,
    RegisterView,
    RegistrationView,
    RestrictionView,
    StagedDocumentView,
    StatutoryFlagView,
    UnitView,
    WalkInReserveView,
)

router = SimpleRouter()
router.register("encounters", EmergencyEncounterViewSet, basename="emergency-encounter")
router.register("appointments", AppointmentViewSet, basename="desk-appointment")

urlpatterns = [
    path(
        "patients/search/",
        PatientNumberSearchView.as_view(),
        name="patient-number-search",
    ),
    path(
        "patients/<uuid:patient_id>/contact/",
        PatientContactView.as_view(),
        name="patient-contact",
    ),
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
    path("documents/", DocumentView.as_view(), name="desk-documents"),
    path(
        "documents/<uuid:document_id>/",
        StagedDocumentView.as_view(),
        name="desk-staged-document",
    ),
    path(
        "restricted/<uuid:patient_id>/",
        RestrictionView.as_view(),
        name="patient-restriction",
    ),
    path(
        "restricted/<uuid:patient_id>/flags/",
        StatutoryFlagView.as_view(),
        name="patient-statutory-flags",
    ),
    path(
        "restricted/<uuid:patient_id>/break-glass/",
        BreakGlassView.as_view(),
        name="patient-break-glass",
    ),
    *router.urls,
]
