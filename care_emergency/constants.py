from django.db import models


class TriageChoices(models.TextChoices):
    red = "red", "Red - Immediate"
    yellow = "yellow", "Yellow - Urgent"
    green = "green", "Green - Delayed"
    black = "black", "Black - Deceased on Arrival"


# Care's Encounter.priority has no "deceased on arrival" value, so Black keeps
# the encounter at emergency priority and the triage itself lives on the plug.
TRIAGE_PRIORITY = {
    TriageChoices.red: "emergency",
    TriageChoices.yellow: "urgent",
    TriageChoices.green: "routine",
    TriageChoices.black: "emergency",
}

MLC_NATURES = [
    "Road accident",
    "Assault",
    "Burn",
    "Poisoning",
    "Suicide attempt",
    "Bite",
    "Sexual assault",
    "Fall",
]
