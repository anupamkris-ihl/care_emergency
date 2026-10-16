# care_emergency

A [Care](https://github.com/ohcnetwork/care) plug for emergency registration (triage, medico-legal (MLC) records and the accompanying person) and front-desk registration (every desk field in its own column) and appointment details (APT number, consultation type, visit purpose, walk-in reserve, phone-booking reminders), kept in the plug's own tables so core Care models and migrations are unchanged. The design is in `hmis_fe/docs/emergency-module-plan.md`.

## Endpoints

Mounted by Care at `/api/care_emergency/`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `desk/?facility=<id>` | The facility's emergency desk number (404 if not set) |
| `PUT` | `desk/` | Set it: `{facility, phone_number}`. Facility admins only (`can_update_facility`). |
| `POST` | `register/` | Create the patient, the `emer` encounter and the emergency record in one transaction. Allocates the MLC number. |
| `GET` | `encounters/?facility=<id>` | List. Filters: `triage`, `is_medico_legal`, `is_unidentified`, `mlc_number`, `chief_complaint`, `mode_of_arrival`, `patient`. |
| `GET` / `PATCH` | `encounters/<encounter_id>/` | Read, or edit triage, MLC, chief complaint, mode of arrival and accompanying-person fields. `mlc_number` is read-only. |
| `POST` | `patients/<patient_id>/convert/` | Convert to a normal patient. The body is a Care patient update and must include the patient's own `phone_number`. |
| `POST` | `registration/` | Front-desk registration: creates the Care patient and its `PatientRegistration` row in one transaction. |
| `GET` | `registration/<patient_id>/` | The patient's front-desk registration (404 if they weren't registered at the desk). Needs view access to the patient. |
| `POST` | `appointments/` | Book a slot through Care's own booking and add the appointment details in one transaction. |
| `GET` | `appointments/?facility=<id>` | List. Filters: `patient`, `appointment_number`, `date`, `booking_channel`. |
| `GET` | `appointments/<booking_id>/` | One booking's appointment details, by the Care booking id. |
| `GET` / `PUT` | `unit/` | Read (`?facility=<id>`) or set `{facility, code}`, the unit code printed in patient numbers (`LN`, 2–4 capitals). Facility admins only; can't be changed once set. |
| `POST` | `patients/<patient_id>/uhid/` | Issue the patient's UHID if they have none: `{facility}`. Refused for unidentified emergency patients until they are converted. |
| `GET` / `PUT` | `walk-in-reserve/` | Read (`?availability=<id>`) or set `{availability, walk_in_every}` for an availability. `walk_in_every: null` removes it. Needs schedule write access. |
| `GET` | `pincode/<pin>/` | India Post's `{country, state, district, localities}` for a PIN, from the plug's directory. 404 if it isn't listed or the directory isn't loaded (`directory_loaded` says which). |

`register/` body:

```json
{
  "facility": "<uuid>",
  "patient": { "name": "Unknown Male", "gender": "male", "age": 35, "registration": {} },
  "organizations": [],
  "triage": "red",
  "is_medico_legal": true,
  "mlc_nature": "Road accident",
  "police_station": "Central",
  "distinguishing_marks": "Scar on left arm",
  "chief_complaint": "Trauma - road traffic",
  "mode_of_arrival": "108 ambulance",
  "is_unidentified": true,
  "brought_by": { "name": "Ravi", "phone_number": "+919898989898", "relationship": "Friend" }
}
```

- `patient` goes through Care's own `PatientCreateSpec` and `PatientViewSet`. Its `phone_number` is always replaced with the desk number, so the person who brought the patient in can't see the record through the OTP patient portal.
- Age must be 0–120 unless a `date_of_birth` is sent.
- Quick registration (`is_unidentified: false`, the patient's real name in `patient`) needs a `chief_complaint`; an unknown patient doesn't. Options are `CHIEF_COMPLAINTS` and `ARRIVAL_MODES` in `constants.py`; `mode_of_arrival` is optional.
- `is_medico_legal` defaults to `false`. When it is `true`, `mlc_nature` and `police_station` are required.
- `brought_by` needs a `name` (who brought the patient, or where from) or a `phone_number` (the attendant's contact); the other parts are optional.
- The encounter is `encounter_class: emer`, `status: in_progress`, `admit_source: emd`. Priority comes from triage: red → `emergency`, yellow → `urgent`, green → `routine`, black → `emergency`. Black does **not** set `deceased_datetime`.
- MLC numbers are `MLC-<year>-<nnnn>` per facility. They are allocated from a locked counter row and are unique in the database. Once issued, a number is kept even if the case is later marked non-medico-legal.

## Front-desk registration

`registration/` body: `{"facility": "<uuid or null>", "patient": {...}, "registration": {...}}`.

- `patient` goes through Care's own `PatientCreateSpec` and `PatientViewSet` (name, gender, phone, date of birth or age, address, PIN, blood group, emergency phone, identifiers).
- `registration` is `RegistrationSpec` in `specs.py`: identity, contact (who the main mobile and the WhatsApp number belong to, `do_not_contact`), permanent address (house to PIN; also send its text line as the patient's `permanent_address`), next of kin, ID summary (type and last 4 only) and ABHA, travel documents, FRRO C-Form, interpreter language, allergies (several, stored as `RegistrationAllergy` rows), assistance, pregnancy, VIP (note, authoriser, handle with care; `vip_flagged_by` is the registering user), payer (with `staff_category` for staff payers) and concession, arrival and lead source, camp and referrer, remarks, consents and contact channels.
- The rules follow the POH front-desk v12.1 spec: under-18s need an exact date of birth, a guardian and a next of kin; every international patient (INT, NRI) needs the country of origin, a passport (a refugee may give the UNHCR card instead), the address in India, an Indian alternate mobile and the international tariff consent; a foreign national also needs the visa status and, unless it is OCI — visa-free or Nepal / Bhutan, the visa validity date (passport expiry and C-Form are optional); main, WhatsApp and next-of-kin numbers may be from any country; the three core consents are required; option fields only take the values in `constants.py`.

## Patient numbers

Every number carries the registering unit's code, so a facility needs a unit code (`unit/`) before desk or emergency registration works. Numbers come from locked counter rows (`PatientNumberSequence`) that only go up, so a number is never issued twice, even if the patient is deleted. Each series is stored as a Care patient identifier on an instance-wide config the plug creates on first use, with `auto_maintained: true` so nobody can edit it through Care.

| Series | Format | Issued |
| --- | --- | --- |
| UHID | `POH-LN-0010294` | At desk registration; at the first encounter in a unit with a code for any patient without one (pre-registrations, patients from Care's own form, older patients); on emergency conversion; or through `patients/<id>/uhid/` |
| Pre-registration | `PRE-LN-0088213` | Desk registration with `"pre_registration": true`. No UHID until arrival or payment. Kept on the record afterwards. |
| Unknown patient | `UNK-LN-20260908-001` | Emergency registration with `is_unidentified: true`. The counter resets daily and the patient is named `Unknown <n>` after it. No UHID until registration is completed through `convert/`. The UNK number is kept on the record afterwards. |

Every unit starts at `0000001`; numbers from an earlier system are not carried over. Never lower a counter's `last_value` in the admin, since that issues numbers again.

Patients created before the plug (or before a facility had a unit code) get their numbers with:

```bash
python manage.py issue_patient_numbers --default-facility <facility id> [--dry-run]
```

Oldest patients are numbered first. Each patient is numbered at the facility of their desk registration, emergency record or first encounter, else `--default-facility`. Unidentified emergency patients not yet converted get an UNK number dated their arrival. Patients that already have a number are skipped, so it can be re-run.

## Appointments

Care's `TokenBooking` only stores the patient, note and tags, and a plug can't extend it, so the v12.1 appointment fields (D46, D48, D49, D51, I04) live in `AppointmentDetails`, one row per booking.

`appointments/` body: `{"slot": "<uuid>", "patient": "<uuid>", "note": "", "tags": [], "booking_channel": "front_desk", "consultation_type": "in_person", "visit_purpose": null}`. The response is `{"booking": <Care booking>, "details": {...}}`.

- The booking itself goes through Care's `SlotViewSet.create_appointment_handler`, so capacity, duplicate and per-patient limits, tags and the consultation charge item work as they do in Care. If Care rejects the booking, no details row is written and no number is used.
- **Appointment number (I04):** `APT-<yy>-<nnnnnn>`, for example `APT-26-000123`. Numbers come from a locked counter row per year that all facilities share, because the format has no site code. The counter resets each year.
- **Consultation type (D46):** `in_person`, `video` or `home_visit`. At check-in (a Care encounter created with `appointment`), an `amb` encounter takes its class from this: video → `vr`, home visit → `hh`. If the desk picked any other class, such as an admission, that class is kept.
- **Visit purpose (D48):** `new`, `follow_up`, `report_review` or `procedure_review`. If it isn't sent, the booking is a follow-up when the patient had a booking with the same resource within the schedule's `revisit_allowed_days`. Otherwise it's `new`.
- **Booking channel:** `walk_in`, `front_desk`, `phone` or `online`. Walk-ins can only be booked for today.
- **Walk-in reserve (D49):** with `walk_in_every: 4`, one seat in four of that availability's seats each day (slots × tokens per slot, excluding availability exceptions) is held back. Scheduled bookings past that cap are refused, and walk-ins can still take the held seats. The cap only applies to bookings made through this endpoint. Care's own `create_appointment` and slot stats don't know about it.
- **Phone-booking reminder (D51):** phone bookings get `reminder_due_at`, set 2 hours before the slot. The `send_appointment_reminders` Celery task runs every 5 minutes. It sends an SMS through Care's SMS backend to the patient's phone for bookings that are still `booked`, and records `reminder_sent_at`.

## PIN directory

India Post's PIN directory (D17, M01) is kept in `PostOffice`: one row per post office, with the office name (the locality, its `B.O`/`S.O` suffix removed), district and state. The source is the Department of Posts' "All India Pincode Directory" on data.gov.in.

- **With a key:** set `DATA_GOV_IN_API_KEY` (an env var on backend and celery, or in the plug's `configs`). Get one by signing up at data.gov.in and copying the key from *My Account*. The `refresh_pincode_directory` Celery task reloads the directory from the data.gov.in API at 03:00 on the 5th of every month. Run `python manage.py load_pincode_directory` to load it the first time.
- **Without a key:** the task logs that it was skipped. The directory can still be loaded from the CSV download with `python manage.py load_pincode_directory --file <csv>`.
- Each load replaces the whole table in one transaction. A download that fails or stops before the API's `total` keeps the old directory.
- hmis_fe asks `pincode/<pin>/` first. On any error, including an unloaded directory or a PIN added after the last refresh, it falls back to a live lookup at api.postalpincode.in.

## Billing

`python manage.py setup_billing [--facility <id>]` seeds Care's billing masters from the POH 2026-27 tariff (front-desk v11 `billing.md`). It runs for every facility with a unit code if no facility is given. Re-running updates in place.

- **Tariff:** root category `poh-tariff`, heads H1–H7 and WARD (SAC in the description), and the item catalogue in `billing.py` as charge item definitions with slugs `poh-<code>`. Per-doctor consultation fees aren't seeded.
- **Payer tariff:** saving a registration tags the patient (*Billing payer*). Corporate gets 15% off list (×0.85), Govt scheme 40% (×0.6), staff 50% (×0.5) and International, INT and NRI a 90% surcharge (×1.9). Insurance and self-pay pay list.
- **Concessions:** staff self get a further 50% (consultation free, other heads 25% of list), dependants 50% on every head. A self-pay patient's chosen senior, freedom fighter or veteran concession adds 15%, 25% or 10%. Care's discounts don't compound, so all are expressed as a share of list.
- **Discount codes:** coupons, discretionary amounts and percentages, and 100% entitlements are added to the facility's discount codes, keeping any codes already there.
- **Invoice numbers:** the plug numbers every invoice `INV/<unit>/<yy>/NNNNN` (item-return invoices `CN/…`), restarting each year under a row lock. The first number of a year follows the highest one already issued. Care's own `invoice_number_expression` is still set, but only for its preview.

### Billing rules

`billing_rules.py` checks Care's charge items, invoices and payments as they're saved, in facilities with a unit code. A broken rule is refused with a 400 from Care's own endpoint. Charge item rules apply only to POH catalogue lines, so pharmacy items are untouched.

- **Quantity:** a whole number from 1 to 20. Ward days aren't capped.
- **Registration fee:** charged once in a lifetime, at any POH facility. Later REG lines get the 100% *registration already paid* entitlement. A cancelled REG line doesn't count.
- **Follow-up:** a consultation (CON2, VID, HOMEV) on an encounter whose booking is a follow-up inside the schedule's revisit window gets the 100% *follow-up* entitlement.
- **Entitlements** replace any concession on the line, as v11 prices them.
- **SAC:** each line's `code` is the SAC of its head (999312 for H1, 999316 otherwise), and it is copied onto the invoice line.
- **Payments:** amounts in whole rupees, under ₹1 crore. Each one gets a number in `extensions.billing.number`: `RCT/…` for payments, `ADV/…` for advances, `RFD/…` for refunds (credit-note payments). The number is kept when the payment is updated.
- **Advances** need `extensions.billing.purpose`: OPD procedure, IP admission, Day-care, IVF package, Health check package or Home care. An unknown patient (UNK number, no UHID yet) can't pay an advance.
- **Refunds** need `extensions.billing.cause` (`hospital` or `patient`) and a `reason` from the v11 list. A patient-caused refund above ₹500 needs a supervisor, meaning someone allowed to cancel payments (`can_destroy_payment_reconciliation`).

Care already refuses edits to an issued invoice or its lines, and it requires a payment method on every payment.

Care can't split a bill between patient and payer, cap or expire coupons, or require approval for discounts; those need a bill screen.

## Local development

Care's `docker-compose.local.yaml` mounts this checkout at `/plugs/care_emergency` and adds it to `PYTHONPATH`. `plug_config.py` registers the app.

```bash
docker exec care-backend-1 python manage.py migrate care_emergency
docker exec care-backend-1 python manage.py test care_emergency --keepdb
docker exec care-backend-1 python manage.py issue_patient_numbers --default-facility <id>
docker exec care-backend-1 python manage.py setup_billing
docker exec care-backend-1 python manage.py load_pincode_directory  # needs DATA_GOV_IN_API_KEY, or --file <csv>
```

## Deploying to a Care instance

Care installs plugs with pip while it builds the image (`docker/prod.Dockerfile` runs `install_plugins.py`). It also reads the plug list again at startup to build `INSTALLED_APPS`. So the plug has to be **in the image** and **in the runtime environment**.

```bash
PLUGS='[{"name":"care_emergency","package_name":"git+https://github.com/anupamkris-ihl/care_emergency.git","version":"@main"}]'

# 1. Build (from a Care checkout)
docker build -f docker/prod.Dockerfile --build-arg ADDITIONAL_PLUGS="$PLUGS" -t care:emergency .

# 2. Run: set the same value as an env var on backend, celery worker and celery beat
ADDITIONAL_PLUGS=$PLUGS

# 3. Migrate (celery beat's start script already runs migrate)
python manage.py migrate care_emergency
```

Alternatively, add the plug to `plug_config.py` in a Care fork. That covers both build and runtime, with no env var. Pin `version` to a tag (for example `@v0.11.0`) rather than `@main` for production.
