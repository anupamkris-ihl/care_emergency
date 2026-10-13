"""India Post's PIN directory (D17, M01), kept as a local master.

The source is the Department of Posts' "All India Pincode Directory" on
data.gov.in, refreshed monthly by a Celery task when an API key is set
(DATA_GOV_IN_API_KEY in the environment, or the plug's configs). Without a key
the table can still be loaded from the directory's CSV download with
`manage.py load_pincode_directory --file`, and while it is empty the frontend
falls back to a live lookup.
"""

import csv
import logging
import os
import re
from collections import Counter

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from care_emergency.models import PostOffice

logger = logging.getLogger(__name__)

# "All India Pincode Directory till last month" (Department of Posts).
RESOURCE_ID = "5c2f62fe-5afa-4119-a499-fec9d604d5bd"
PAGE_SIZE = 1000

# Column names seen in the API records and the CSV download, lowercased.
COLUMNS = {
    "pincode": ("pincode",),
    "name": ("officename", "office_name", "office name"),
    "office_type": ("officetype", "office_type", "office type"),
    "delivery": ("delivery", "deliverystatus"),
    "district": ("district", "districtname", "district_name"),
    "state": ("statename", "state_name", "state"),
}

# "Connaught Place S.O", "Achampet B.O", "New Delhi G.P.O." → the locality.
OFFICE_SUFFIX = re.compile(r"\s+(?:B|S|H|G\.?\s?P|P)\.?\s?O\.?$", re.IGNORECASE)
SMALL_WORDS = {"and", "of", "the"}


class DirectoryError(Exception):
    pass


def plug_setting(name):
    configs = getattr(settings, "PLUGIN_CONFIGS", {}).get("care_emergency", {})
    return configs.get(name) or os.environ.get(name, "")


def api_key():
    return plug_setting("DATA_GOV_IN_API_KEY").strip()


def tidy(text):
    """Directory text is often all capitals ("NEW DELHI"); show it as a name."""
    text = " ".join(str(text or "").split())
    if not text.isupper():
        return text
    words = text.lower().split(" ")
    return " ".join(
        word if i and word in SMALL_WORDS else word.capitalize()
        for i, word in enumerate(words)
    )


def parse_row(raw):
    """A directory record as PostOffice fields, or None if it isn't usable."""
    row = {str(key).strip().lower(): value for key, value in raw.items() if key}

    def pick(field):
        for column in COLUMNS[field]:
            if row.get(column) not in (None, ""):
                return str(row[column]).strip()
        return ""

    pincode = pick("pincode")
    name = OFFICE_SUFFIX.sub("", tidy(pick("name")))
    if not re.fullmatch(r"[1-9]\d{5}", pincode) or not name:
        return None
    return {
        "pincode": pincode,
        "name": name[:100],
        "office_type": pick("office_type")[:10],
        "delivery": not pick("delivery").lower().startswith("non"),
        "district": tidy(pick("district"))[:100],
        "state": tidy(pick("state"))[:100],
    }


def rows_from_api(key, session=None):
    """Every directory record, page by page. Raises unless all of them arrive,
    so a partial download never replaces the table."""
    session = session or requests.Session()
    url = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
    offset, total = 0, None
    while total is None or offset < total:
        try:
            response = session.get(
                url,
                params={
                    "api-key": key,
                    "format": "json",
                    "offset": offset,
                    "limit": PAGE_SIZE,
                },
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as error:
            msg = f"data.gov.in request failed at record {offset}: {error}"
            raise DirectoryError(msg) from error
        records = body.get("records") or []
        total = int(body.get("total") or 0)
        if not records:
            break
        yield from records
        offset += len(records)
    if not total or offset < total:
        msg = f"data.gov.in returned {offset} of {total} records"
        raise DirectoryError(msg)


def rows_from_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as file:
        yield from csv.DictReader(file)


def replace_directory(records):
    """Replaces the whole directory in one transaction; returns the row count."""
    now = timezone.now()
    offices, seen = [], set()
    for record in records:
        row = parse_row(record)
        if row is None or (row["pincode"], row["name"]) in seen:
            continue
        seen.add((row["pincode"], row["name"]))
        offices.append(PostOffice(**row, loaded_at=now))
    if not offices:
        msg = "The directory has no usable rows"
        raise DirectoryError(msg)
    with transaction.atomic():
        PostOffice.objects.all().delete()
        PostOffice.objects.bulk_create(offices, batch_size=5000)
    return len(offices)


def refresh_from_api():
    """Reloads the directory from data.gov.in; None when no key is set."""
    key = api_key()
    if not key:
        return None
    return replace_directory(rows_from_api(key))


def address_problem(pincode, locality, district, state):
    """Why an address doesn't fit its PIN (R17), or "" when it does. A PIN the
    directory doesn't list passes: the desk types that address by hand (R18)."""
    offices = list(
        PostOffice.objects.filter(pincode=pincode).values("name", "district", "state")
    )
    if not offices:
        return ""

    def key(text):
        return " ".join(text.split()).casefold()

    if not locality:
        return "Choose the PIN's post office"
    if key(locality) not in {key(office["name"]) for office in offices}:
        return "The post office is not under this PIN"
    places = {(key(office["district"]), key(office["state"])) for office in offices}
    if (key(district), key(state)) not in places:
        return "The district and state do not match the PIN"
    return ""


def lookup(pincode):
    """The PIN's district, state and localities, or None if it isn't listed."""
    offices = list(
        PostOffice.objects.filter(pincode=pincode).values("name", "district", "state")
    )
    if not offices:
        return None
    # A PIN almost always sits in one district; if not, the commonest wins.
    district, state = Counter(
        (office["district"], office["state"]) for office in offices
    ).most_common(1)[0][0]
    return {
        "country": "India",
        "state": state,
        "district": district,
        "localities": sorted({office["name"] for office in offices}),
    }
