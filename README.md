# care_emergency

A [Care](https://github.com/ohcnetwork/care) plug for emergency registration (triage, medico-legal (MLC) records and the accompanying person) and front-desk registration (every desk field in its own column) and appointment details (APT number, consultation type, visit purpose, walk-in reserve, phone-booking reminders), kept in the plug's own tables so core Care models and migrations are unchanged. The design is in `hmis_fe/docs/emergency-module-plan.md`.

## Endpoints

Mounted by Care at `/api/care_emergency/`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `desk/?facility=<id>` | The facility's emergency desk number (404 if not set) |
| `PUT` | `desk/` | Set it: `{facility, phone_number}`. Facility admins only (`can_update_facility`). |
| `POST` | `register/` | Create the patient, the `emer` encounter and the emergency record in one transaction. Allocates the MLC number. |
| `GET` | `encounters/?facility=<id>` | List. Filters: `triage`, `is_medico_legal`, `is_unidentified`, `mlc_number`, `patient`. |
| `GET` / `PATCH` | `encounters/<encounter_id>/` | Read, or edit triage, MLC and accompanying-person fields. `mlc_number` is read-only. |
| `POST` | `patients/<patient_id>/convert/` | Convert to a normal patient. The body is a Care patient update and must include the patient's own `phone_number`. |
| `POST` | `registration/` | Front-desk registration: creates the Care patient and its `PatientRegistration` row in one transaction. |
| `GET` | `registration/<patient_id>/` | The patient's front-desk registration (404 if they weren't registered at the desk). Needs view access to the patient. |
| `POST` | `appointments/` | Book a slot through Care's own booking and add the appointment details in one transaction. |
| `GET` | `appointments/?facility=<id>` | List. Filters: `patient`, `appointment_number`, `date`, `booking_channel`. |
| `GET` | `appointments/<booking_id>/` | One booking's appointment details, by the Care booking id. |
| `GET` / `PUT` | `unit/` | Read (`?facility=<id>`) or set `{facility, code}`, the unit code printed in patient numbers (`LN`, 2–4 capitals). Facility admins only; can't be changed once set. |
| `POST` | `patients/<patient_id>/uhid/` | Issue the patient's UHID if they have none: `{facility}`. Refused for unidentified emergency patients until they are converted. |
| `GET` / `PUT` | `walk-in-reserve/` | Read (`?availability=<id>`) or set `{availability, walk_in_every}` for an availability. `walk_in_every: null` removes it. Needs schedule write access. |

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
  "is_unidentified": true,
  "brought_by": { "name": "Ravi", "phone_number": "+919898989898", "relationship": "Friend" }
}
```

- `patient` goes through Care's own `PatientCreateSpec` and `PatientViewSet`. Its `phone_number` is always replaced with the desk number, so the person who brought the patient in can't see the record through the OTP patient portal.
- Age must be 1–130 unless a `date_of_birth` is sent. Care returns a 500 for `age: 0`.
- The encounter is `encounter_class: emer`, `status: in_progress`, `admit_source: emd`. Priority comes from triage: red → `emergency`, yellow → `urgent`, green → `routine`, black → `emergency`. Black does **not** set `deceased_datetime`.
- MLC numbers are `MLC-<year>-<nnnn>` per facility. They are allocated from a locked counter row and are unique in the database. Once issued, a number is kept even if the case is later marked non-medico-legal.

## Front-desk registration

`registration/` body: `{"facility": "<uuid or null>", "patient": {...}, "registration": {...}}`.

- `patient` goes through Care's own `PatientCreateSpec` and `PatientViewSet` (name, gender, phone, date of birth or age, address, PIN, blood group, emergency phone, identifiers).
- `registration` is `RegistrationSpec` in `specs.py`: identity, contact, next of kin, ID summary (type and last 4 only) and ABHA, travel documents, FRRO C-Form, interpreter language, allergies (several, stored as `RegistrationAllergy` rows), assistance, pregnancy, VIP (note, authoriser, handle with care; `vip_flagged_by` is the registering user), payer and concession, arrival and lead source, camp and referrer, remarks, consents and contact channels.
- The rules follow the POH front-desk v12.1 spec: under-18s need an exact date of birth, a guardian and a next of kin; foreign nationals need passport, visa and C-Form; the three core consents are required; option fields only take the values in `constants.py`.

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

## Local development

Care's `docker-compose.local.yaml` mounts this checkout at `/plugs/care_emergency` and adds it to `PYTHONPATH`. `plug_config.py` registers the app.

```bash
docker exec care-backend-1 python manage.py migrate care_emergency
docker exec care-backend-1 python manage.py test care_emergency --keepdb
docker exec care-backend-1 python manage.py issue_patient_numbers --default-facility <id>
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

Alternatively, add the plug to `plug_config.py` in a Care fork. That covers both build and runtime, with no env var. Pin `version` to a tag (for example `@v0.3.0`) rather than `@main` for production.
