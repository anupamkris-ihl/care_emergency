from care.emr.resources.base import PhoneNumber
from pydantic import UUID4, BaseModel, Field, model_validator

from care_emergency.constants import MLC_NATURES, TriageChoices


class BroughtBySpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_number: PhoneNumber | None = Field(default=None, max_length=14)
    relationship: str = Field(default="", max_length=50)


class DeskWriteSpec(BaseModel):
    facility: UUID4
    phone_number: PhoneNumber = Field(max_length=14)


class MedicoLegalMixin:
    def check_medico_legal(self, is_medico_legal, mlc_nature, police_station):
        if is_medico_legal:
            if mlc_nature not in MLC_NATURES:
                raise ValueError("Select the MLC nature")
            if not police_station.strip():
                raise ValueError("Enter the police station informed")


class EmergencyRegisterSpec(MedicoLegalMixin, BaseModel):
    facility: UUID4
    # Passed through to Care's PatientCreateSpec; phone_number is replaced by
    # the facility's emergency desk number.
    patient: dict
    organizations: list[UUID4] = []
    triage: TriageChoices
    is_medico_legal: bool
    mlc_nature: str = ""
    police_station: str = Field(default="", max_length=200)
    distinguishing_marks: str = Field(default="", max_length=500)
    is_unidentified: bool = True
    brought_by: BroughtBySpec | None = None

    @model_validator(mode="after")
    def validate_medico_legal(self):
        self.check_medico_legal(
            self.is_medico_legal, self.mlc_nature, self.police_station
        )
        if not self.is_medico_legal:
            self.mlc_nature = ""
            self.police_station = ""
        return self

    @model_validator(mode="after")
    def validate_age(self):
        # Care raises a 500 for age=0 without a date of birth
        # (PatientCreateSpec.perform_extra_deserialization), so require an
        # estimate of at least 1 year.
        age = self.patient.get("age")
        if self.patient.get("date_of_birth"):
            return self
        if not isinstance(age, int) or isinstance(age, bool) or not 1 <= age <= 130:  # noqa: PLR2004
            raise ValueError("Enter an estimated age between 1 and 130")
        return self


class EmergencyUpdateSpec(BaseModel):
    triage: TriageChoices | None = None
    is_medico_legal: bool | None = None
    mlc_nature: str | None = None
    police_station: str | None = Field(default=None, max_length=200)
    distinguishing_marks: str | None = Field(default=None, max_length=500)
    brought_by: BroughtBySpec | None = None
