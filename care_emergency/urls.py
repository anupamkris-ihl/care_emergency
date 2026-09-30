from django.urls import path
from rest_framework.routers import SimpleRouter

from care_emergency.views import (
    ConvertView,
    DeskView,
    EmergencyEncounterViewSet,
    RegisterView,
)

router = SimpleRouter()
router.register("encounters", EmergencyEncounterViewSet, basename="emergency-encounter")

urlpatterns = [
    path("desk/", DeskView.as_view(), name="emergency-desk"),
    path("register/", RegisterView.as_view(), name="emergency-register"),
    path(
        "patients/<uuid:patient_id>/convert/",
        ConvertView.as_view(),
        name="emergency-convert",
    ),
    *router.urls,
]
