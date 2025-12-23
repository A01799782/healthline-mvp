import os
import uuid
import json
from datetime import datetime, timedelta
from flask import Flask, redirect, render_template, request, url_for
try:
    from PIL import Image
except ImportError:
    Image = None

import db
from services.dose_status import compute_event_status
from services.rxnorm import fetch_suggestions
from authz import CARE_ADMIN, NURSE, get_current_role, require_roles

app = Flask(__name__)


db.init_db()

UPLOAD_DIR = os.path.join(app.root_path, "static", "uploads", "patients")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def save_patient_photo(file_storage, old_path: str | None = None):
    if Image is None:
        return None
    allowed = {"jpg", "jpeg", "png", "webp"}
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed:
        return None
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    if size > 5 * 1024 * 1024:
        return None
    file_storage.stream.seek(0)
    try:
        img = Image.open(file_storage.stream)
        img = img.convert("RGB")
        w, h = img.size
        m = min(w, h)
        left = (w - m) // 2
        top = (h - m) // 2
        img = img.crop((left, top, left + m, top + m))
        img = img.resize((256, 256))
        filename = f"{uuid.uuid4().hex}.jpg"
        path = os.path.join(UPLOAD_DIR, filename)
        img.save(path, format="JPEG", quality=85)
        if old_path:
            try:
                os.remove(os.path.join(app.root_path, "static", old_path))
            except Exception:
                pass
        return f"uploads/patients/{filename}"
    except Exception:
        return None


@app.context_processor
def inject_helpers():
    def fmt_dt(value: str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return value

    return {"fmt_dt": fmt_dt, "now": db.now, "current_role": get_current_role(request)}


@app.route("/")
def patient_list():
    patients = db.list_patients()
    msg = request.args.get("msg", "")
    now_iso = db.now().isoformat()
    fall_counts = {p["id"]: db.count_falls_last_90_days(p["id"], now_iso) for p in patients}
    return render_template("patient_list.html", patients=patients, msg=msg, fall_counts=fall_counts)


@app.route("/patients/new", methods=["GET", "POST"])
@require_roles(CARE_ADMIN, NURSE)
def patient_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        notes = request.form.get("notes", "").strip() or None
        dob = request.form.get("date_of_birth", "").strip() or None
        diagnosis = request.form.get("diagnosis", "").strip() or None
        allergies = request.form.get("allergies", "").strip() or None
        ec_name = request.form.get("emergency_contact_name", "").strip() or None
        ec_phone = request.form.get("emergency_contact_phone", "").strip() or None
        ec_relation = request.form.get("emergency_contact_relation", "").strip() or None
        photo_path = None
        if "photo" in request.files:
            photo_file = request.files["photo"]
            if photo_file and photo_file.filename:
                photo_path = save_patient_photo(photo_file)
        if name:
            pid = db.add_patient(
                name,
                notes,
                date_of_birth=dob,
                diagnosis=diagnosis,
                allergies=allergies,
                emergency_contact_name=ec_name,
                emergency_contact_phone=ec_phone,
                emergency_contact_relation=ec_relation,
                photo_path=photo_path,
            )
            db.log_audit(
                "create_patient",
                "patient",
                pid,
                get_current_role(request),
                {"notes": bool(notes)},
            )
            return redirect(url_for("patient_list"))
    return render_template("patient_new.html")


@app.route("/patients/<int:patient_id>")
def patient_detail(patient_id: int):
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    age = db.calculate_age(patient.get("date_of_birth"))
    msg = request.args.get("msg", "")
    fall_count = db.count_falls_last_90_days(patient_id, db.now().isoformat())
    now_dt = db.now()
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    seven_start = now_dt - timedelta(days=7)
    thirty_start = now_dt - timedelta(days=30)
    adherence_today = db.get_adherence_summary(patient_id, db.to_db_timestamp(day_start), db.to_db_timestamp(day_end), db.to_db_timestamp(now_dt))
    adherence_7d = db.get_adherence_summary(patient_id, db.to_db_timestamp(seven_start), db.to_db_timestamp(now_dt), db.to_db_timestamp(now_dt))
    adherence_30d = db.get_adherence_summary(patient_id, db.to_db_timestamp(thirty_start), db.to_db_timestamp(now_dt), db.to_db_timestamp(now_dt))
    denom_7d = adherence_7d["taken_due"] + adherence_7d["skipped_due"] + adherence_7d["overdue_due"]
    denom_30d = adherence_30d["taken_due"] + adherence_30d["skipped_due"] + adherence_30d["overdue_due"]
    adherence_pct_7d = round(adherence_7d["taken_due"] * 100 / denom_7d, 1) if denom_7d > 0 else None
    adherence_pct_30d = round(adherence_30d["taken_due"] * 100 / denom_30d, 1) if denom_30d > 0 else None
    meds = db.list_medications_for_patient(patient_id)
    meds_with_events = []
    for m in meds:
        events = db.list_dose_events_for_medication(m["id"], limit=5)
        med_copy = dict(m)
        end_raw = med_copy.get("end_time")
        if end_raw:
            try:
                med_copy["ended"] = datetime.fromisoformat(end_raw) <= db.now()
            except ValueError:
                med_copy["ended"] = False
        else:
            med_copy["ended"] = False
        meds_with_events.append((med_copy, events))
    return render_template(
        "patient_detail.html",
        patient=patient,
        meds_with_events=meds_with_events,
        age=age,
        msg=msg,
        fall_count=fall_count,
        adherence_today=adherence_today,
        adherence_7d=adherence_7d,
        adherence_30d=adherence_30d,
        adherence_pct_7d=adherence_pct_7d,
        adherence_pct_30d=adherence_pct_30d,
    )


@app.route("/medications/new", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def medication_new():
    patient_id = int(request.form.get("patient_id"))
    name = request.form.get("name", "").strip()
    dose_value = request.form.get("dose_value", "").strip()
    dose_unit = request.form.get("dose_unit", "").strip()
    dose = f"{dose_value} {dose_unit}".strip() if dose_unit and dose_unit != "otro" else dose_value
    freq = request.form.get("frequency_hours", "0").strip()
    notes = request.form.get("notes", "").strip() or None
    start_time_raw = request.form.get("start_time", "").strip()
    end_time_raw = request.form.get("end_time", "").strip()
    rxnorm_rxcui = request.form.get("rxnorm_rxcui", "").strip() or None
    rxnorm_name = request.form.get("rxnorm_name", "").strip() or None
    if not name or not dose or not freq:
        return redirect(url_for("patient_detail", patient_id=patient_id))
    try:
        frequency_hours = int(freq)
    except ValueError:
        return redirect(url_for("patient_detail", patient_id=patient_id))
    if start_time_raw:
        try:
            start_time = datetime.fromisoformat(start_time_raw)
        except ValueError:
            start_time = db.now()
    else:
        start_time = db.now()
    end_time = None
    if end_time_raw:
        try:
            end_time = datetime.fromisoformat(end_time_raw)
        except ValueError:
            end_time = None
    med_id = db.add_medication(
        patient_id,
        name,
        dose,
        frequency_hours,
        notes,
        start_time,
        end_time=end_time,
        rxnorm_rxcui=rxnorm_rxcui,
        rxnorm_name=rxnorm_name or name,
        dose_value=dose_value or None,
        dose_unit=dose_unit or None,
    )
    db.log_audit(
        "create_medication",
        "medication",
        med_id,
        get_current_role(request),
        {
            "patient_id": patient_id,
            "frequency_hours": frequency_hours,
            "has_end_time": bool(end_time),
            "rxnorm_selected": bool(rxnorm_rxcui),
            "rxnorm_rxcui": rxnorm_rxcui,
            "dose_unit": dose_unit or None,
            "dose_value": dose_value or None,
        },
    )
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/alerts")
def alerts():
    patient_name = request.args.get("patient_name", "").strip()
    events = db.list_upcoming_dose_events(patient_name=patient_name if patient_name else None)
    current_time = db.now()
    enriched = []
    for ev in events:
        status_data = compute_event_status(ev, current_time)
        ev["status"] = status_data["status"]
        ev["_order_bucket"] = status_data["order_bucket"]
        ev["_order_time"] = status_data["sched_dt"]
        ev["display_dose"] = (
            f"{ev.get('medication_dose_value')} {ev.get('medication_dose_unit')}".strip()
            if ev.get("medication_dose_value") and ev.get("medication_dose_unit")
            else ev.get("medication_dose")
        )
        enriched.append(ev)
    enriched.sort(key=lambda e: (e["_order_bucket"], e["_order_time"]))
    patient_names = db.get_patient_names()
    return render_template("alerts.html", events=enriched, patient_name=patient_name, patient_names=patient_names)


@app.route("/api/rxnorm/suggest")
def rxnorm_suggest():
    query = request.args.get("query", "").strip()
    now_iso = db.now().isoformat()
    suggestions = fetch_suggestions(query, now_iso)
    return json.dumps(suggestions), 200, {"Content-Type": "application/json"}


@app.route("/patients/<int:patient_id>/today")
def patient_today(patient_id: int):
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    current = db.now()
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    events = db.list_patient_events_for_day(patient_id, db.to_db_timestamp(day_start), db.to_db_timestamp(day_end))
    enriched = []
    for ev in events:
        status_data = compute_event_status(ev, current)
        ev["status"] = status_data["status"]
        ev["_order_bucket"] = status_data["order_bucket"]
        ev["_order_time"] = status_data["sched_dt"]
        ev["_hour_label"] = status_data["hour_label"]
        ev["display_dose"] = (
            f"{ev.get('medication_dose_value')} {ev.get('medication_dose_unit')}".strip()
            if ev.get("medication_dose_value") and ev.get("medication_dose_unit")
            else ev.get("medication_dose")
        )
        enriched.append(ev)
    enriched.sort(key=lambda e: (e["_order_bucket"], e["_order_time"]))
    # group by hour label preserving order
    grouped = []
    last_label = None
    for ev in enriched:
        if ev["_hour_label"] != last_label:
            grouped.append({"hour": ev["_hour_label"], "events": []})
            last_label = ev["_hour_label"]
        grouped[-1]["events"].append(ev)
    return render_template(
        "patient_today.html",
        patient=patient,
        grouped_events=grouped,
    )


@app.route("/dose_events/<int:event_id>/take", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def take_dose(event_id: int):
    db.mark_dose_taken(event_id)
    db.log_audit("take_dose", "dose_event", event_id, get_current_role(request), None)
    return redirect(request.referrer or url_for("alerts"))


@app.route("/dose_events/<int:event_id>/skip", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def skip_dose(event_id: int):
    db.mark_dose_skipped(event_id)
    db.log_audit("skip_dose", "dose_event", event_id, get_current_role(request), None)
    return redirect(request.referrer or url_for("alerts"))


@app.route("/dose_events/<int:event_id>/undo", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def undo_dose(event_id: int):
    db.undo_dose_event(event_id)
    db.log_audit("undo_dose", "dose_event", event_id, get_current_role(request), None)
    return redirect(request.referrer or url_for("alerts"))


@app.route("/dose_events/<int:event_id>/note", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def note_dose(event_id: int):
    note = request.form.get("note", "").strip() or None
    db.save_dose_note(event_id, note)
    db.log_audit("note_dose", "dose_event", event_id, get_current_role(request), {"note_len": len(note) if note else 0})
    return redirect(request.referrer or url_for("alerts"))


@app.route("/adherence")
def adherence_dashboard():
    patients = db.list_patients()
    now_iso = db.to_db_timestamp(db.now())
    seven_start = (datetime.fromisoformat(now_iso) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for p in patients:
        adherence_7d = db.get_adherence_summary(p["id"], seven_start, now_iso, now_iso)
        taken = adherence_7d["taken_due"]
        skipped = adherence_7d["skipped_due"]
        overdue = adherence_7d["overdue_due"]
        denom = taken + skipped + overdue
        percent = round(taken * 100 / denom, 1) if denom > 0 else None
        overdue = db.get_overdue_count(p["id"], now_iso)
        falls = db.count_falls_last_90_days(p["id"], now_iso)
        rows.append(
            {
                "patient": p,
                "overdue": overdue,
                "adherence_percent": percent,
                "taken": taken,
                "skipped": skipped,
                "falls": falls,
            }
        )
    rows.sort(key=lambda r: (-r["overdue"], r["adherence_percent"] if r["adherence_percent"] is not None else 101))
    return render_template("adherence.html", rows=rows)


@app.route("/dev/overdue/<int:patient_id>")
def dev_overdue(patient_id: int):
    if not (app.debug or app.env == "development"):
        return "Not found", 404
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    now_iso = db.now().isoformat()
    events = db.list_overdue_pending_events(patient_id, now_iso)
    return {
        "patient": patient["name"],
        "now": now_iso,
        "overdue_pending": events,
    }


@app.route("/patients/<int:patient_id>/delete", methods=["POST"])
@require_roles(CARE_ADMIN)
def patient_delete(patient_id: int):
    db.delete_patient(patient_id)
    db.log_audit("delete_patient", "patient", patient_id, get_current_role(request), None)
    return redirect(url_for("patient_list"))


@app.route("/patients/<int:patient_id>/edit", methods=["GET", "POST"])
@require_roles(CARE_ADMIN, NURSE)
def patient_edit(patient_id: int):
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        notes = request.form.get("notes", "").strip() or None
        dob = request.form.get("date_of_birth", "").strip() or None
        diagnosis = request.form.get("diagnosis", "").strip() or None
        allergies = request.form.get("allergies", "").strip() or None
        ec_name = request.form.get("emergency_contact_name", "").strip() or None
        ec_phone = request.form.get("emergency_contact_phone", "").strip() or None
        ec_relation = request.form.get("emergency_contact_relation", "").strip() or None
        photo_path = None
        if "photo" in request.files:
            photo_file = request.files["photo"]
            if photo_file and photo_file.filename:
                photo_path = save_patient_photo(photo_file, old_path=patient.get("photo_path"))
        if not name:
            return render_template("patient_edit.html", patient=patient, error="El nombre es obligatorio.")
        db.update_patient(
            patient_id,
            name,
            notes,
            date_of_birth=dob,
            diagnosis=diagnosis,
            allergies=allergies,
            emergency_contact_name=ec_name,
            emergency_contact_phone=ec_phone,
            emergency_contact_relation=ec_relation,
            photo_path=photo_path,
        )
        db.log_audit("update_patient", "patient", patient_id, get_current_role(request), {"has_notes": bool(notes)})
        return redirect(url_for("patient_detail", patient_id=patient_id))
    return render_template("patient_edit.html", patient=patient, error=None)


@app.route("/patients/<int:patient_id>/falls/new", methods=["GET", "POST"])
@require_roles(CARE_ADMIN, NURSE)
def fall_new(patient_id: int):
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    if request.method == "POST":
        occurred_raw = request.form.get("occurred_at", "").strip()
        location = request.form.get("location", "").strip()
        note = request.form.get("note", "").strip() or None
        if not location:
            return render_template(
                "falls_new.html",
                patient=patient,
                error="El lugar es obligatorio.",
                default_dt=occurred_raw or db.now().strftime("%Y-%m-%dT%H:%M"),
            )
        try:
            occurred_dt = datetime.fromisoformat(occurred_raw) if occurred_raw else db.now()
        except ValueError:
            occurred_dt = db.now()
        db.add_fall_event(patient_id, occurred_dt, location, note)
        msg = "Caída registrada"
        suggestion = ""
        if patient.get("emergency_contact_name") or patient.get("emergency_contact_phone"):
            suggestion = f"Sugerencia: avisar al contacto de emergencia: {patient.get('emergency_contact_name') or ''} ({patient.get('emergency_contact_phone') or ''})"
            msg = f"{msg}. {suggestion}".strip()
        db.log_audit(
            "create_fall_event",
            "fall_event",
            None,
            get_current_role(request),
            {"patient_id": patient_id, "location": location},
        )
        return redirect(url_for("patient_detail", patient_id=patient_id, msg=msg))
    default_dt = db.now().strftime("%Y-%m-%dT%H:%M")
    return render_template("falls_new.html", patient=patient, error=None, default_dt=default_dt)


@app.route("/patients/<int:patient_id>/falls")
def falls_list(patient_id: int):
    patient = db.get_patient(patient_id)
    if not patient:
        return "Paciente no encontrado", 404
    events = db.list_fall_events(patient_id)
    return render_template("falls_list.html", patient=patient, events=events)


@app.route("/medications/<int:med_id>/edit", methods=["GET", "POST"])
@require_roles(CARE_ADMIN, NURSE)
def medication_edit(med_id: int):
    med = db.get_medication(med_id)
    if not med:
        return "Medicamento no encontrado", 404
    patient_id = med["patient_id"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dose = request.form.get("dose", "").strip()
        dose_value = request.form.get("dose_value", "").strip()
        dose_unit = request.form.get("dose_unit", "").strip()
        if dose_value and dose_unit and dose_unit != "otro":
            dose = f"{dose_value} {dose_unit}".strip()
        freq_raw = request.form.get("frequency_hours", "").strip()
        notes = request.form.get("notes", "").strip() or None
        start_raw = request.form.get("start_time", "").strip()
        end_raw = request.form.get("end_time", "").strip()
        rxnorm_rxcui = request.form.get("rxnorm_rxcui", "").strip() or None
        rxnorm_name = request.form.get("rxnorm_name", "").strip() or None
        error = None
        if not name:
            error = "El nombre es obligatorio."
        if not dose:
            error = "La dosis es obligatoria."
        try:
            frequency_hours = int(freq_raw)
            if frequency_hours <= 0:
                raise ValueError
        except ValueError:
            error = "La frecuencia debe ser un número mayor a 0."
        start_time = None
        if start_raw:
            try:
                start_time = datetime.fromisoformat(start_raw)
            except ValueError:
                error = error or "Formato de hora inicial inválido."
        end_time = None
        if end_raw:
            try:
                end_time = datetime.fromisoformat(end_raw)
            except ValueError:
                error = error or "Formato de hora fin inválido."
        if error:
            return render_template("medication_edit.html", med=med, patient_id=patient_id, error=error)
        db.update_medication(
            med_id,
            name,
            dose,
            frequency_hours,
            notes,
            start_time,
            end_time,
            active=med.get("active"),
            rxnorm_rxcui=rxnorm_rxcui,
            rxnorm_name=rxnorm_name or name,
            dose_value=dose_value or None,
            dose_unit=dose_unit or None,
        )
        db.reset_future_events(med_id, seed_time=start_time)
        db.log_audit(
            "update_medication",
            "medication",
            med_id,
            get_current_role(request),
        {
            "patient_id": patient_id,
            "frequency_hours": frequency_hours,
            "has_end_time": bool(end_time),
            "rxnorm_selected": bool(rxnorm_rxcui),
            "rxnorm_rxcui": rxnorm_rxcui,
            "dose_unit": dose_unit or None,
            "dose_value": dose_value or None,
        },
    )
        return redirect(url_for("patient_detail", patient_id=patient_id))
    return render_template("medication_edit.html", med=med, patient_id=patient_id, error=None)


@app.route("/medications/<int:med_id>/toggle_active", methods=["POST"])
@require_roles(CARE_ADMIN, NURSE)
def medication_toggle_active(med_id: int):
    med = db.get_medication(med_id)
    if not med:
        return "Medicamento no encontrado", 404
    patient_id = med["patient_id"]
    new_active = 0 if med.get("active", 1) else 1
    db.update_medication(
        med_id,
        med["name"],
        med["dose"],
        med["frequency_hours"],
        med.get("notes"),
        datetime.fromisoformat(med["start_time"]) if med.get("start_time") else None,
        datetime.fromisoformat(med["end_time"]) if med.get("end_time") else None,
        active=new_active,
    )
    if new_active == 0:
        db.delete_pending_events_for_medication(med_id)
    else:
        db.ensure_next_dose_event(med_id)
    db.log_audit(
        "toggle_medication_active",
        "medication",
        med_id,
        get_current_role(request),
        {"patient_id": patient_id, "active": new_active},
    )
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/dev/seed", methods=["POST"])
@require_roles(CARE_ADMIN)
def dev_seed():
    if not (app.debug or app.env == "development"):
        return "Not found", 404
    db.seed_demo()
    db.log_audit("seed_demo", "seed", None, get_current_role(request), None)
    return redirect(url_for("patient_list", msg="Demo data loaded"))


if __name__ == "__main__":
    app.run(debug=True)
