# care_emergency

A [Care](https://github.com/ohcnetwork/care) plug for emergency registration: triage, medico-legal (MLC) records and the accompanying person, kept in the plug's own tables so core Care models and migrations are unchanged. The design is in `hmis_fe/docs/emergency-module-plan.md`.

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

`register/` body:

```json
{
  "facility": "<uuid>",
  "patient": { "name": "Unknown Male", "gender": "male", "age": 35, "registration": {} },
  "organizations": [],
  "triage": "red",
  "is_medico_legal": true,
  "mlc_nature": "Road traffic accident",
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

## Local development

Care's `docker-compose.local.yaml` mounts this checkout at `/plugs/care_emergency` and adds it to `PYTHONPATH`. `plug_config.py` registers the app.

```bash
docker exec care-backend-1 python manage.py migrate care_emergency
docker exec care-backend-1 python manage.py test care_emergency --keepdb
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

Alternatively, add the plug to `plug_config.py` in a Care fork. That covers both build and runtime, with no env var. Pin `version` to a tag (for example `@v0.1.0`) rather than `@main` for production.
