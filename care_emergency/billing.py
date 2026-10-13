"""Front desk billing masters (front-desk v11 billing.md, Scope M19, D31, D36,
D55, INV).

setup_billing() loads them into Care's own billing models for one facility:

- the service heads H1–H7 as resource categories under one tariff root,
- the v11 catalogue as charge item definitions,
- payer-type tariffs and fixed concessions as price components on the root
  (and H1), applied by Care when the patient carries the matching tag,
- coupons, discretionary discounts and entitlements as facility discount
  definitions the cashier can pick,
- the INV/{unit}/{yy}/NNNNN invoice number.

sync_billing_tags() keeps a patient's payer and concession tags in step with
their desk registration. Payer shares (insurance 90/10, credit to the payer),
coupon caps and validity, discount approvals and entitlement rules are not
expressible in Care's pricing and stay outside this module."""

from decimal import Decimal

from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.facility_config import FacilityMonetoryConfig
from care.emr.models.resource_category import (
    ResourceCategory,
    merge_monetary_components,
)
from care.emr.models.tag_config import TagConfig
from care.emr.resources.facility.spec import FacilityMonetaryCodeSpec
from django.db import transaction

from care_emergency.constants import INTERNATIONAL_TYPES
from care_emergency.services import unit_code

DISCOUNT_SYSTEM = "http://ohc.network/codes/monetary/discount"
SURCHARGE_SYSTEM = "http://ohc.network/codes/monetary/surcharge"

# Patient tags, one per root: Care applies at most one tag from each.
PAYER_TAGS = {
    "corporate": "Corporate — CORP-26 ×0.85",
    "scheme": "Govt scheme — CGHS-26 ×0.6",
    "staff_self": "POH staff — self — STAFF-26 ×0.5",
    "staff_dependant": "POH staff — dependant — STAFF-26 ×0.5",
    "international": "International — INTL-26 ×1.9",
}
CONCESSION_TAGS = {
    "senior": "Senior citizen — 15%",
    "freedom_fighter": "Freedom fighter — 25%",
    "veteran": "Defence veteran — 10%",
    "ews": "EWS / charity",
    "discretionary": "Hospital discretionary",
}
PAYER_ROOT = "Billing payer"
CONCESSION_ROOT = "Billing concession"
# Desk registration values → concession tag keys.
CONCESSION_VALUES = {
    "Senior citizen — 15%": "senior",
    "Freedom fighter — 25%": "freedom_fighter",
    "Defence veteran — 10%": "veteran",
    "EWS / charity": "ews",
    "Hospital discretionary": "discretionary",
}

# Price build-up (billing.md §2). Care computes every discount on the line's
# rate, so the staff concession is expressed on the list price: 50% of the
# ×0.5 staff rate is 25% of list, and a free consultation is the other 50%.
TARIFF_COMPONENTS = [
    ("tariff_corporate", "Corporate contract CORP-26", "discount", 15, "corporate"),
    ("tariff_scheme", "Scheme rate CGHS-26", "discount", 40, "scheme"),
    ("tariff_staff_self", "Staff tariff STAFF-26", "discount", 50, "staff_self"),
    (
        "tariff_staff_dependant",
        "Staff tariff STAFF-26",
        "discount",
        50,
        "staff_dependant",
    ),
    (
        "tariff_international",
        "International tariff INTL-26",
        "surcharge",
        90,
        "international",
    ),
]
CONCESSION_COMPONENTS = [
    ("concession_staff_self", "Staff concession — 50%", 25, "staff_self"),
    (
        "concession_staff_dependant",
        "Staff dependant concession — 50%",
        25,
        "staff_dependant",
    ),
    ("concession_senior", "Senior citizen concession — 15%", 15, "senior"),
    (
        "concession_freedom_fighter",
        "Freedom fighter concession — 25%",
        25,
        "freedom_fighter",
    ),
    ("concession_veteran", "Defence veteran concession — 10%", 10, "veteran"),
]
# Staff — self: consultation free (H1).
H1_STAFF_SELF = (
    "concession_staff_self",
    "Staff concession — consultation free",
    50,
    "staff_self",
)

ROOT_CATEGORY = ("poh-tariff", "POH tariff 2026-27", "Rates in force from 01-Apr-2026.")
HEADS = {
    "H1": ("Registration & consultation", "999312"),
    "H2": ("Diagnostics", "999316"),
    "H3": ("Procedures & vaccination", "999316"),
    "H4": ("Emergency", "999316"),
    "H5": ("Home care", "999316"),
    "H6": ("Packages", "999316"),
    "H7": ("Other services", "999316"),
    "WARD": ("Wards (per day)", "999316"),
}

# (code, head, title, list price, description)
CATALOGUE = [
    ("REG", "H1", "Registration — one time", 200, "Once in a lifetime"),
    ("CON2", "H1", "Referred consultation", 900, "Same day, different specialty"),
    ("FU", "H1", "Follow-up inside the free window", 0, "Doctor's 7 or 14 day window"),
    ("VID", "H1", "Video / tele consultation", 700, None),
    ("HOMEV", "H1", "Doctor home visit", 2500, None),
    # Laboratory
    ("CBC", "H2", "Complete blood count", 350, None),
    ("LFT", "H2", "Liver function test", 700, None),
    ("KFT", "H2", "Kidney function test", 800, None),
    ("LIP", "H2", "Lipid profile", 950, None),
    ("HBA1C", "H2", "HbA1c", 600, None),
    ("TSH", "H2", "Thyroid — TSH", 450, None),
    ("FBS", "H2", "Blood sugar — fasting", 120, None),
    ("UR", "H2", "Urine routine", 200, None),
    ("VITD", "H2", "Vitamin D", 1400, None),
    ("HBSAG", "H2", "HBsAg", 450, "Sensitive: printed, sealed, handed over in person"),
    (
        "VDRL",
        "H2",
        "VDRL / syphilis",
        300,
        "Sensitive: printed, sealed, handed over in person",
    ),
    ("B12", "H2", "Vitamin B12", 900, None),
    ("CRP", "H2", "CRP", 550, None),
    ("DENG", "H2", "Dengue NS1", 650, None),
    ("SEMEN", "H2", "Semen analysis", 800, None),
    ("AMH", "H2", "AMH", 2200, None),
    ("BHCG", "H2", "Beta-hCG", 900, None),
    (
        "HIV",
        "H2",
        "HIV 1 & 2 (with counselling)",
        500,
        "Sensitive: printed, sealed, handed over in person",
    ),
    ("TORCH", "H2", "TORCH panel", 2600, None),
    ("PT", "H2", "PT / INR", 450, None),
    ("HCV", "H2", "Anti-HCV", 750, "Sensitive: printed, sealed, handed over in person"),
    ("HSC", "H2", "Home sample collection", 200, None),
    # Imaging
    ("XR-CHEST", "H2", "X-ray chest PA", 450, None),
    ("XR-KNEE", "H2", "X-ray knee AP/Lat", 650, None),
    ("XR-LSPINE", "H2", "X-ray lumbar spine", 750, None),
    ("USG-ABD", "H2", "USG abdomen", 1400, None),
    ("USG-PELVIS", "H2", "USG pelvis", 1200, None),
    ("USG-OBS", "H2", "USG obstetric", 1600, "Female only; PC-PNDT Form F"),
    ("USG-FOLL", "H2", "Follicular study", 900, "Female only"),
    ("CT-HEAD", "H2", "CT head plain", 2800, None),
    ("CT-CHEST", "H2", "CT chest", 4500, None),
    ("MRI-BRAIN", "H2", "MRI brain", 6500, None),
    ("MRI-KNEE", "H2", "MRI knee", 6000, None),
    ("ECG", "H2", "ECG 12-lead", 300, None),
    ("ECHO", "H2", "2D Echo", 2200, None),
    ("TMT", "H2", "TMT", 2000, None),
    ("MAMMO", "H2", "Mammography bilateral", 2400, None),
    ("DEXA", "H2", "DEXA scan", 2000, None),
    # Procedures
    ("DRESS-S", "H3", "Dressing small", 250, None),
    ("DRESS-L", "H3", "Dressing large", 450, None),
    ("INJ-IM", "H3", "Injection IM/SC", 150, None),
    ("INJ-IV", "H3", "Injection IV", 250, None),
    ("NEB", "H3", "Nebulisation", 250, None),
    ("SUTURE-R", "H3", "Suture removal", 300, None),
    ("CATH", "H3", "Catheter change", 600, None),
    ("IVINF", "H3", "IV infusion up to 2 h", 900, None),
    # Vaccination
    ("VAC-HEPB", "H3", "Hepatitis B", 450, "All ages"),
    ("VAC-DTP", "H3", "DTwP / DTaP", 900, "Child schedule (under 18)"),
    ("VAC-MMR", "H3", "MMR", 800, "Child schedule (under 18)"),
    ("VAC-PCV13", "H3", "Pneumococcal PCV13", 3800, "Child schedule (under 18)"),
    ("VAC-ROTA", "H3", "Rotavirus", 1800, "Child schedule (under 18)"),
    ("VAC-TD", "H3", "Td (adult tetanus)", 250, "Adult schedule"),
    ("VAC-HPV", "H3", "HPV (Gardasil 9)", 10850, "Adult schedule"),
    ("VAC-FLU", "H3", "Influenza 2026-27", 1600, "All ages"),
    ("VAC-PPSV23", "H3", "Pneumococcal PPSV23 (60+)", 2600, "Adult schedule"),
    ("VAC-TCV", "H3", "Typhoid conjugate", 2200, "All ages"),
    ("VAC-RABIES", "H3", "Anti-rabies", 450, "All ages"),
    ("VAC-SHINGRIX", "H3", "Shingles (Shingrix)", 10500, "Adult schedule"),
    ("VAC-ADMIN", "H3", "Vaccine administration", 150, "Added to every vaccine"),
    # Emergency
    ("ER-ATT", "H4", "Emergency attendance", 1500, None),
    ("ER-OBS", "H4", "Observation per hour", 400, None),
    ("ER-RESUS", "H4", "Resuscitation", 5000, None),
    # Home care
    ("HC-NURSE", "H5", "Nursing visit", 900, None),
    ("HC-PHYSIO", "H5", "Physiotherapy session", 1100, None),
    ("HC-SAMPLE", "H5", "Home sample collection", 200, None),
    ("HC-DOCTOR", "H5", "Doctor home visit", 2500, None),
    ("HC-WOUND", "H5", "Wound care at home", 1200, None),
    ("HC-INJ", "H5", "Injection at home", 600, None),
    ("HC-ELDER12", "H5", "Elderly care 12 hours", 2400, None),
    ("HC-TRAVEL-0", "H5", "Travel 0–5 km", 0, None),
    ("HC-TRAVEL-5", "H5", "Travel 5–10 km", 150, None),
    ("HC-TRAVEL-10", "H5", "Travel 10–15 km", 300, None),
    # Packages
    (
        "PKG-ESS",
        "H6",
        "POH Essential check",
        2500,
        "CBC, FBS, Lipid, Urine, ECG, Physician",
    ),
    (
        "PKG-GOLD",
        "H6",
        "POH Gold check",
        5500,
        "CBC, LFT, KFT, Lipid, HbA1c, TSH, Urine, ECG, X-ray chest, USG abdomen, Physician",
    ),
    (
        "PKG-WMN",
        "H6",
        "Women's wellness",
        4800,
        "Female only. CBC, TSH, Vit D, USG pelvis, Mammography, Gynaecologist",
    ),
    (
        "PKG-SNR",
        "H6",
        "Senior citizen check",
        4200,
        "CBC, KFT, Lipid, HbA1c, ECG, Echo, DEXA, Physician",
    ),
    (
        "PKG-CARD",
        "H6",
        "Cardiac risk check",
        6500,
        "Lipid, HbA1c, ECG, Echo, TMT, Cardiologist",
    ),
    (
        "PKG-PRE",
        "H6",
        "Pre-employment check",
        1200,
        "CBC, Urine, X-ray chest, Physician",
    ),
    (
        "PKG-CORP",
        "H6",
        "Corporate annual check",
        3400,
        "Corporate payer only. CBC, LFT, KFT, Lipid, HbA1c, ECG, Physician",
    ),
    # Other
    ("MEDCERT", "H7", "Medical certificate", 300, None),
    ("DUPREPORT", "H7", "Duplicate report / film", 100, None),
    ("AMB-5KM", "H7", "Ambulance within 5 km", 1200, None),
    ("FITCERT", "H7", "Fitness certificate", 500, None),
    # Wards, per day (free beds in brackets in v11 are capacity, not price)
    ("WARD-GEN", "WARD", "General ward", 2000, None),
    ("WARD-TWIN", "WARD", "Twin sharing", 3800, None),
    ("WARD-PVT", "WARD", "Private room", 6500, None),
    ("WARD-DELUXE", "WARD", "Deluxe suite", 11000, None),
    ("WARD-ICU", "WARD", "ICU", 18000, None),
    ("WARD-NICU", "WARD", "NICU", 12000, "Newborn only"),
    ("WARD-DAYCARE", "WARD", "Day-care bed", 2500, None),
]

# Facility discounts the cashier picks (billing.md §5): (code, display, factor %, amount ₹)
PICKED_DISCOUNTS = [
    ("coupon_firstvisit", "Coupon FIRSTVISIT — registration free", None, 200),
    ("coupon_senior10", "Coupon SENIOR10 — 10% on diagnostics", 10, None),
    ("coupon_diwali20", "Coupon DIWALI20 — 20% on procedures & packages", 20, None),
    ("coupon_app100", "Coupon APP100 — ₹100 off consultation", None, 100),
    ("discretionary_100", "Discretionary — ₹100", None, 100),
    ("discretionary_200", "Discretionary — ₹200", None, 200),
    ("discretionary_500", "Discretionary — ₹500", None, 500),
    ("discretionary_1000", "Discretionary — ₹1,000", None, 1000),
    ("discretionary_10", "Discretionary — 10%", 10, None),
    ("discretionary_25", "Discretionary — 25%", 25, None),
    ("discretionary_waiver", "Discretionary — full waiver", 100, None),
    ("entitlement_registration", "Entitlement — registration already paid", 100, None),
    (
        "entitlement_follow_up",
        "Entitlement — follow-up inside the free window",
        100,
        None,
    ),
    (
        "entitlement_statutory",
        "Entitlement — statutory free treatment (BNSS s.397)",
        100,
        None,
    ),
]


def slug_for(code):
    return f"poh-{code.lower()}"


def coding(system, code, display):
    return {"system": system, "code": code, "display": display}


def tag_condition(tag):
    return {
        "metric": "patient_tag",
        "operation": "has_tag",
        "value": {"value": str(tag.external_id)},
    }


def ensure_tags(facility, root_display, children):
    root, _ = TagConfig.objects.get_or_create(
        facility=facility,
        resource="patient",
        display=root_display,
        parent=None,
        defaults={
            "status": "active",
            "category": "admin",
            "description": "Front desk billing",
        },
    )
    tags = {}
    for key, display in children.items():
        tag = TagConfig.objects.filter(
            facility=facility, resource="patient", parent=root, display=display
        ).first()
        if tag is None:
            tag = TagConfig(
                facility=facility,
                resource="patient",
                parent=root,
                display=display,
                status="active",
                category="admin",
                description="Front desk billing",
            )
            tag.save()
        tags[key] = tag
    return tags


def price_component(code, title, kind, factor, tag):
    system = SURCHARGE_SYSTEM if kind == "surcharge" else DISCOUNT_SYSTEM
    return {
        "monetary_component_type": kind,
        "code": coding(system, code, title),
        "factor": str(factor),
        "conditions": [tag_condition(tag)],
    }


def ensure_category(facility, slug_value, title, description, parent, components):
    slug = ResourceCategory.calculate_slug_from_facility(
        facility.external_id, slug_value
    )
    category = ResourceCategory.objects.filter(facility=facility, slug=slug).first()
    if category is None:
        category = ResourceCategory(
            facility=facility,
            slug=slug,
            resource_type="charge_item_definition",
            resource_sub_type="other",
            parent=parent,
            is_child=parent is not None,
        )
    category.title = title
    category.description = description
    category.configured_monetary_components = components
    category.calculated_monetary_components = (
        merge_monetary_components(parent.calculated_monetary_components, components)
        if parent
        else components
    )
    category.save()
    return category


def ensure_definition(facility, category, code, title, price, description):
    slug = ChargeItemDefinition.calculate_slug_from_facility(
        facility.external_id, slug_for(code)
    )
    definition = ChargeItemDefinition.objects.filter(
        facility=facility, slug=slug
    ).first()
    if definition is None:
        definition = ChargeItemDefinition(facility=facility, slug=slug)
    definition.status = "active"
    definition.title = title
    definition.description = description
    definition.purpose = code
    definition.category = category
    definition.price_components = [
        {"monetary_component_type": "base", "amount": str(Decimal(price))}
    ]
    definition.can_edit_charge_item = False
    definition.save()
    return definition


def configure_facility(facility, unit):
    config = FacilityMonetoryConfig.get_monetory_config(facility.id)
    ours = {code for code, *_ in PICKED_DISCOUNTS}
    ours |= {code for code, *_ in TARIFF_COMPONENTS} | {
        code for code, *_ in CONCESSION_COMPONENTS
    }
    codes = [c for c in config.discount_codes if c.get("code") not in ours]
    definitions = [
        d
        for d in config.discount_monetary_components
        if (d.get("code") or {}).get("code") not in ours
    ]
    for code, title, kind, *_ in TARIFF_COMPONENTS:
        if kind == "discount":
            codes.append(coding(DISCOUNT_SYSTEM, code, title))
    for code, title, *_ in CONCESSION_COMPONENTS:
        codes.append(coding(DISCOUNT_SYSTEM, code, title))
    for code, display, factor, amount in PICKED_DISCOUNTS:
        codes.append(coding(DISCOUNT_SYSTEM, code, display))
        definition = {
            "title": display,
            "monetary_component_type": "discount",
            "code": coding(DISCOUNT_SYSTEM, code, display),
        }
        if factor is not None:
            definition["factor"] = str(factor)
        else:
            definition["amount"] = str(amount)
        definitions.append(definition)
    # Same checks as Care's facility discount API.
    FacilityMonetaryCodeSpec.model_validate(
        {
            "discount_codes": codes,
            "discount_monetary_components": definitions,
            "discount_configuration": config.discount_configuration or None,
        }
    )
    config.discount_codes = codes
    config.discount_monetary_components = definitions
    config.invoice_number_expression = (
        f'f"INV/{unit}/{{current_year_yy}}/{{invoice_count + 1:05d}}"'
    )
    config.save()
    return config


@transaction.atomic
def setup_billing(facility):
    """Create or refresh the billing masters for one facility. Safe to rerun:
    everything is matched by slug or display and updated in place."""
    unit = unit_code(facility)
    payer_tags = ensure_tags(facility, PAYER_ROOT, PAYER_TAGS)
    concession_tags = ensure_tags(facility, CONCESSION_ROOT, CONCESSION_TAGS)
    tags = {**payer_tags, **concession_tags}

    root_components = [
        price_component(code, title, kind, factor, tags[tag])
        for code, title, kind, factor, tag in TARIFF_COMPONENTS
    ]
    root_components += [
        price_component(code, title, "discount", factor, tags[tag])
        for code, title, factor, tag in CONCESSION_COMPONENTS
    ]
    slug_value, title, description = ROOT_CATEGORY
    root = ensure_category(
        facility, slug_value, title, description, None, root_components
    )

    categories = {}
    for head, (name, sac) in HEADS.items():
        components = []
        if head == "H1":
            code, title, factor, tag = H1_STAFF_SELF
            components.append(
                price_component(code, title, "discount", factor, tags[tag])
            )
        categories[head] = ensure_category(
            facility,
            slug_for(head),
            f"{head} {name}" if head != "WARD" else name,
            f"SAC {sac}",
            root,
            components,
        )

    for code, head, title, price, description in CATALOGUE:
        ensure_definition(facility, categories[head], code, title, price, description)

    configure_facility(facility, unit)
    return {
        "categories": len(categories) + 1,
        "definitions": len(CATALOGUE),
        "tags": len(tags),
    }


def billing_tag_keys(registration):
    """The payer and concession tags a registration earns (billing.md §2).
    Insurance and plain self-pay are priced at the standard tariff: no tag."""
    keys = []
    if (
        registration.registration_type in INTERNATIONAL_TYPES
        or registration.payer_type == "International"
    ):
        keys.append("international")
    elif registration.payer_type == "Corporate":
        keys.append("corporate")
    elif registration.payer_type == "Govt scheme":
        keys.append("scheme")
    elif registration.payer_type == "Staff":
        keys.append(
            "staff_dependant"
            if registration.payer_name == "POH staff — dependant"
            else "staff_self"
        )
    if (
        registration.payer_type == "Self"
        and registration.concession in CONCESSION_VALUES
    ):
        keys.append(CONCESSION_VALUES[registration.concession])
    return keys


def sync_billing_tags(registration):
    """Replace the patient's billing tags at the registration's facility. Does
    nothing until setup_billing has run there."""
    facility = registration.facility
    if facility is None:
        return
    managed = TagConfig.objects.filter(
        facility=facility,
        resource="patient",
        parent__display__in=[PAYER_ROOT, CONCESSION_ROOT],
        parent__facility=facility,
    )
    by_display = {tag.display: tag.id for tag in managed}
    if not by_display:
        return
    displays = {**PAYER_TAGS, **CONCESSION_TAGS}
    wanted = [
        by_display[displays[key]]
        for key in billing_tag_keys(registration)
        if displays[key] in by_display
    ]
    patient = registration.patient
    facility_tags = dict(patient.facility_tags or {})
    current = facility_tags.get(str(facility.id), [])
    managed_ids = set(by_display.values())
    updated = [tag for tag in current if tag not in managed_ids] + wanted
    if updated == current:
        return
    facility_tags[str(facility.id)] = updated
    patient.facility_tags = facility_tags
    patient.save(update_fields=["facility_tags"])
