"""The lab test catalogue: each lab test is a Care ActivityDefinition (classification
laboratory) linked to its tariff row (ChargeItemDefinition), the sample it needs
(SpecimenDefinition) and the parameters it reports (ObservationDefinitions, LOINC).
POH's own rules (fasting hours, sensitive report) sit beside it in LabTestRule.

The front desk reads the catalogue to book lab tests, and facility admins edit it
from Settings → Lab tests in hmis_fe."""

import re

from care.emr.models import ActivityDefinition
from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.observation_definition import ObservationDefinition
from care.emr.models.specimen_definition import SpecimenDefinition
from django.db import transaction
from pydantic import BaseModel, Field, field_validator, model_validator
from rest_framework.exceptions import ValidationError

from care_emergency.billing import slug_for
from care_emergency.models import LabTestRule

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
SPECIMEN_TYPES = "http://terminology.hl7.org/CodeSystem/v2-0487"

# Care requires a procedure code on every activity definition; lab tests share this one.
LAB_PROCEDURE = {"system": SNOMED, "code": "15220000", "display": "Laboratory test"}
# Set on the sample's patient preparation when the test needs fasting, so Care's own
# specimen record says so too. The hours live in LabTestRule.
AFTER_FASTING = {"system": SNOMED, "code": "726054005", "display": "After fasting"}

# The valuesets the admin's codes must come from (checked when they change).
SPECIMEN_VALUESET = "system-specimen_type-code"
PARAMETER_VALUESET = "system-observation"

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9-]{0,19}$"
MAX_FASTING_HOURS = 24
MAX_PARAMETERS = 60


class CodingSpec(BaseModel):
    system: str
    code: str = Field(min_length=1, max_length=64)
    display: str = Field(min_length=1, max_length=1024)


class ParameterSpec(CodingSpec):
    # The name shown on the test and its report; the LOINC display is often long.
    name: str = Field(min_length=1, max_length=255)

    def coding(self):
        return {"system": self.system, "code": self.code, "display": self.display}


class LabTestWriteSpec(BaseModel):
    facility: str
    title: str = Field(min_length=1, max_length=255)
    status: str = "active"
    charge_item_definition: str
    specimen: CodingSpec
    fasting_hours: int | None = Field(default=None, ge=1, le=MAX_FASTING_HOURS)
    sensitive_report: bool = False
    parameters: list[ParameterSpec] = Field(min_length=1, max_length=MAX_PARAMETERS)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter the test's name")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value):
        if value not in ("active", "retired"):
            raise ValueError("Status must be active or retired")
        return value

    @field_validator("specimen")
    @classmethod
    def specimen_system(cls, value):
        if value.system != SPECIMEN_TYPES:
            raise ValueError("Choose the sample type from the list")
        return value

    @model_validator(mode="after")
    def unique_parameters(self):
        codes = [parameter.code for parameter in self.parameters]
        if any(parameter.system != LOINC for parameter in self.parameters):
            raise ValueError("Parameters must be LOINC codes")
        if len(codes) != len(set(codes)):
            raise ValueError("A parameter is listed twice")
        return self


class LabTestCreateSpec(LabTestWriteSpec):
    code: str

    @field_validator("code")
    @classmethod
    def code_format(cls, value):
        value = value.strip().upper()
        if not re.match(CODE_PATTERN, value):
            raise ValueError(
                "Use up to 20 capital letters, digits and hyphens, e.g. LFT"
            )
        return value


def field_error(field, message):
    """A 400 in the same shape as a Pydantic error, so the form shows it under its field."""
    return ValidationError(
        {"errors": [{"type": "validation_error", "loc": [field], "msg": message}]}
    )


def lab_slug(code):
    return f"poh-lab-{code.lower()}"


def parameter_slug(code):
    return f"loinc-{code.lower()}"


def facility_slug(facility, slug_value):
    # Every definition model shares SlugBaseModel's facility scoping.
    return ActivityDefinition.calculate_slug_from_facility(facility.external_id, slug_value)


def code_from_slug(facility, slug):
    prefix = facility_slug(facility, "poh-lab-")
    return slug[len(prefix) :].upper() if slug.startswith(prefix) else None


def base_price(definition):
    for component in definition.price_components or []:
        if component.get("monetary_component_type") == "base":
            return component.get("amount")
    return None


def serialize_charge_item(definition):
    return {
        "slug": definition.slug,
        "code": definition.purpose,
        "title": definition.title,
        "price": base_price(definition),
        "status": definition.status,
    }


def lab_tests(facility, status=None):
    """The facility's lab tests, each with its fee, sample, rules and parameters,
    loaded in a fixed number of queries."""
    activities = ActivityDefinition.objects.filter(
        facility=facility,
        classification="laboratory",
        slug__startswith=facility_slug(facility, "poh-lab-"),
    ).order_by("title")
    if status:
        activities = activities.filter(status=status)
    activities = list(activities)

    def by_id(model, field):
        ids = {i for activity in activities for i in getattr(activity, field)}
        return {row.id: row for row in model.objects.filter(id__in=ids)}

    charge_items = by_id(ChargeItemDefinition, "charge_item_definitions")
    specimens = by_id(SpecimenDefinition, "specimen_requirements")
    parameters = by_id(ObservationDefinition, "observation_result_requirements")
    rules = {
        rule.activity_definition_id: rule
        for rule in LabTestRule.objects.filter(activity_definition__in=activities)
    }

    rows = []
    for activity in activities:
        charge_item = next(
            (charge_items[i] for i in activity.charge_item_definitions if i in charge_items),
            None,
        )
        specimen = next(
            (specimens[i] for i in activity.specimen_requirements if i in specimens), None
        )
        rule = rules.get(activity.id)
        rows.append(
            {
                "id": str(activity.external_id),
                "code": code_from_slug(facility, activity.slug),
                "title": activity.title,
                "status": activity.status,
                "charge_item_definition": serialize_charge_item(charge_item)
                if charge_item
                else None,
                "specimen": specimen.type_collected if specimen else None,
                "fasting_hours": rule.fasting_hours if rule else None,
                "sensitive_report": rule.sensitive_report if rule else False,
                "parameters": [
                    {**parameters[i].code, "name": parameters[i].title}
                    for i in activity.observation_result_requirements
                    if i in parameters
                ],
            }
        )
    return rows


def lab_test_row(facility, activity):
    return next(row for row in lab_tests(facility) if row["id"] == str(activity.external_id))


def lab_tariff(facility):
    """Tariff rows a lab test can be priced from: the facility's active diagnostics."""
    rows = ChargeItemDefinition.objects.filter(
        facility=facility,
        status="active",
        category__slug=facility_slug(facility, slug_for("h2")),
    ).order_by("title")
    return [serialize_charge_item(row) for row in rows]


def check_codes(facility, spec, activity=None):
    """Checks the codes the admin chose against Care's valuesets. Codes Care already
    holds for this facility were checked when they were first saved, so only new
    ones go to the terminology server."""
    from care.emr.registries.care_valueset.care_valueset import validate_valueset
    from care.emr.resources.common import Coding

    def check(valueset, coding, label):
        try:
            validate_valueset(
                "code",
                valueset,
                Coding.model_validate(
                    {"system": coding.system, "code": coding.code, "display": coding.display}
                ),
            )
        except ValueError as error:
            raise field_error(label, f"{coding.display}: {error}") from error

    known_specimen = activity and SpecimenDefinition.objects.filter(
        id__in=activity.specimen_requirements,
        type_collected__code=spec.specimen.code,
    ).exists()
    if not known_specimen:
        check(SPECIMEN_VALUESET, spec.specimen, "specimen")
    known = set(
        ObservationDefinition.objects.filter(
            facility=facility,
            slug__in=[
                facility_slug(facility, parameter_slug(p.code)) for p in spec.parameters
            ],
        ).values_list("code__code", flat=True)
    )
    for parameter in spec.parameters:
        if parameter.code not in known:
            check(PARAMETER_VALUESET, parameter, "parameters")


def ensure_parameter(facility, spec, user=None):
    """The facility's ObservationDefinition for a LOINC code, shared by every test that
    reports it, so renaming it here renames it everywhere."""
    slug = facility_slug(facility, parameter_slug(spec.code))
    parameter = ObservationDefinition.objects.filter(facility=facility, slug=slug).first()
    if parameter is not None and parameter.title != spec.name:
        parameter.title = spec.name
        parameter.updated_by = user
        parameter.save()
    if parameter is None:
        # Results aren't entered in this app yet, so the type defaults to a number with
        # no reference ranges; result entry will refine both.
        parameter = ObservationDefinition(
            facility=facility,
            slug=slug,
            title=spec.name,
            status="active",
            description="",
            derived_from_uri="",
            category="laboratory",
            code=spec.coding(),
            permitted_data_type="quantity",
            qualified_ranges=[],
            created_by=user,
            updated_by=user,
        )
        parameter.save()
    return parameter


@transaction.atomic
def save_lab_test(facility, spec, user=None, activity=None):
    """Creates (activity=None) or updates a lab test with its sample, parameters and rules."""
    charge_item = ChargeItemDefinition.objects.filter(
        facility=facility, slug=spec.charge_item_definition
    ).first()
    if charge_item is None:
        raise field_error("charge_item_definition", "Choose the tariff item")

    if activity is None:
        slug = facility_slug(facility, lab_slug(spec.code))
        if ActivityDefinition.objects.filter(slug=slug).exists():
            raise field_error("code", f"A lab test with code {spec.code} already exists")
        activity = ActivityDefinition(
            facility=facility,
            slug=slug,
            classification="laboratory",
            kind="service_request",
            code=LAB_PROCEDURE,
            description="",
            usage="",
            locations=[],
            created_by=user,
        )
    else:
        activity.version = (activity.version or 1) + 1
    code = code_from_slug(facility, activity.slug)

    specimen_slug = facility_slug(facility, lab_slug(code))
    specimen = SpecimenDefinition.objects.filter(facility=facility, slug=specimen_slug).first()
    if specimen is None:
        specimen = SpecimenDefinition(
            facility=facility, slug=specimen_slug, description="", created_by=user
        )
    specimen.title = f"{spec.title} — {spec.specimen.display}"
    specimen.status = "active"
    specimen.type_collected = spec.specimen.model_dump()
    specimen.patient_preparation = [AFTER_FASTING] if spec.fasting_hours else []
    specimen.updated_by = user
    specimen.save()

    activity.title = spec.title
    activity.status = spec.status
    activity.charge_item_definitions = [charge_item.id]
    activity.specimen_requirements = [specimen.id]
    activity.observation_result_requirements = [
        ensure_parameter(facility, parameter, user).id for parameter in spec.parameters
    ]
    activity.updated_by = user
    activity.save()

    LabTestRule.objects.update_or_create(
        activity_definition=activity,
        defaults={
            "fasting_hours": spec.fasting_hours,
            "sensitive_report": spec.sensitive_report,
            "updated_by": user,
        },
        create_defaults={
            "fasting_hours": spec.fasting_hours,
            "sensitive_report": spec.sensitive_report,
            "created_by": user,
            "updated_by": user,
        },
    )
    return activity


# The starting catalogue: the lab rows of the v11 tariff (billing.CATALOGUE, head H2),
# with the sample, fasting hours and sensitivity from the front-office spec (KB Module 1
# preparation engine, FO-19) and the LOINC parameters each reports. Admins edit it from
# then on; setup_lab_catalogue only adds tests that aren't there yet.
SAMPLE = {
    "BLD": "Whole blood",
    "SER": "Serum",
    "PLAS": "Plasma",
    "UR": "Urine",
    "SMN": "Seminal fluid",
}

LAB_SEED = [
    ("CBC", "BLD", None, False, [
        ("718-7", "Haemoglobin"), ("4544-3", "Haematocrit (PCV)"), ("789-8", "RBC count"),
        ("787-2", "MCV"), ("785-6", "MCH"), ("786-4", "MCHC"), ("788-0", "RDW"),
        ("6690-2", "Total WBC count"), ("777-3", "Platelet count"), ("32623-1", "MPV"),
        ("770-8", "Neutrophils %"), ("736-9", "Lymphocytes %"), ("5905-5", "Monocytes %"),
        ("713-8", "Eosinophils %"), ("706-2", "Basophils %"), ("751-8", "Absolute neutrophil count"),
        ("731-0", "Absolute lymphocyte count"), ("742-7", "Absolute monocyte count"),
        ("711-2", "Absolute eosinophil count"), ("704-7", "Absolute basophil count"),
    ]),
    ("LFT", "SER", 8, False, [
        ("1975-2", "Bilirubin, total"), ("1968-7", "Bilirubin, direct"), ("1971-1", "Bilirubin, indirect"),
        ("1920-8", "AST (SGOT)"), ("1742-6", "ALT (SGPT)"), ("6768-6", "Alkaline phosphatase"),
        ("2324-2", "GGT"), ("2885-2", "Total protein"), ("1751-7", "Albumin"),
        ("10834-0", "Globulin"), ("1759-0", "A/G ratio"),
    ]),
    ("KFT", "SER", None, False, [
        ("3091-6", "Urea"), ("3094-0", "Blood urea nitrogen"), ("2160-0", "Creatinine"),
        ("3084-1", "Uric acid"), ("2951-2", "Sodium"), ("2823-3", "Potassium"),
        ("2075-0", "Chloride"), ("17861-6", "Calcium"), ("2777-1", "Phosphorus"),
        ("98979-8", "eGFR (CKD-EPI 2021)"),
    ]),
    ("LIP", "SER", 10, False, [
        ("2093-3", "Total cholesterol"), ("2571-8", "Triglycerides"), ("2085-9", "HDL cholesterol"),
        ("13457-7", "LDL cholesterol"), ("13458-5", "VLDL cholesterol"),
        ("9830-1", "Total cholesterol/HDL ratio"), ("43396-1", "Non-HDL cholesterol"),
    ]),
    ("HBA1C", "BLD", None, False, [("4548-4", "HbA1c"), ("27353-2", "Estimated average glucose")]),
    ("TSH", "SER", None, False, [("3016-3", "TSH")]),
    ("FBS", "PLAS", 8, False, [("1558-6", "Fasting blood glucose")]),
    ("UR", "UR", None, False, [
        ("5778-6", "Colour"), ("5767-9", "Appearance"), ("5811-5", "Specific gravity"),
        ("5803-2", "pH"), ("5804-0", "Protein"), ("5792-7", "Glucose"), ("5797-6", "Ketones"),
        ("5770-3", "Bilirubin"), ("5794-3", "Blood"), ("5802-4", "Nitrite"),
        ("5799-2", "Leukocyte esterase"), ("5818-0", "Urobilinogen"),
        ("13945-1", "RBC (microscopy)"), ("5821-4", "Pus cells (microscopy)"),
        ("5787-7", "Epithelial cells (microscopy)"),
    ]),
    ("VITD", "SER", None, False, [("62292-8", "25-OH vitamin D (D2 + D3)")]),
    ("HBSAG", "SER", None, True, [("5196-1", "HBsAg")]),
    ("VDRL", "SER", None, True, [("5292-8", "VDRL")]),
    ("B12", "SER", None, False, [("2132-9", "Vitamin B12")]),
    ("CRP", "SER", None, False, [("1988-5", "C-reactive protein")]),
    ("DENG", "SER", None, False, [("91064-6", "Dengue NS1 antigen")]),
    ("SEMEN", "SMN", None, False, [
        ("3160-9", "Volume"), ("2752-4", "pH"), ("9780-8", "Sperm concentration"),
        ("6800-7", "Motility"), ("10622-9", "Normal morphology"),
    ]),
    ("AMH", "SER", None, False, [("38476-8", "Anti-Müllerian hormone")]),
    ("BHCG", "SER", None, False, [("21198-7", "Beta-hCG")]),
    ("HIV", "SER", None, True, [("31201-7", "HIV 1 & 2 antibodies")]),
    ("TORCH", "SER", None, False, [
        ("8039-0", "Toxoplasma IgG"), ("8040-8", "Toxoplasma IgM"), ("8014-3", "Rubella IgG"),
        ("8015-0", "Rubella IgM"), ("7852-7", "CMV IgG"), ("7853-5", "CMV IgM"),
        ("31411-2", "HSV 1 & 2 IgG"), ("25435-9", "HSV IgM"),
    ]),
    ("PT", "PLAS", None, False, [("5902-2", "Prothrombin time"), ("6301-6", "INR")]),
    ("HCV", "SER", None, True, [("16128-1", "Anti-HCV")]),
]


def seed_lab_catalogue(facility, user=None):
    """Adds the starting lab tests that the facility doesn't have yet. Tests already
    there are left exactly as the admin set them. Needs setup_billing first, for the
    tariff rows."""
    added = 0
    for code, sample, fasting_hours, sensitive, parameters in LAB_SEED:
        if ActivityDefinition.objects.filter(
            slug=facility_slug(facility, lab_slug(code))
        ).exists():
            continue
        charge_item = ChargeItemDefinition.objects.filter(
            facility=facility, slug=facility_slug(facility, slug_for(code))
        ).first()
        if charge_item is None:
            continue
        spec = LabTestCreateSpec(
            facility=str(facility.external_id),
            code=code,
            title=charge_item.title,
            charge_item_definition=charge_item.slug,
            specimen={"system": SPECIMEN_TYPES, "code": sample, "display": SAMPLE[sample]},
            fasting_hours=fasting_hours,
            sensitive_report=sensitive,
            parameters=[
                {"system": LOINC, "code": loinc, "display": name, "name": name}
                for loinc, name in parameters
            ],
        )
        save_lab_test(facility, spec, user)
        added += 1
    return added
