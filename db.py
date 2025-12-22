import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.environ.get("HEALTHLINE_DB", os.path.join(os.path.dirname(__file__), "healthline.db"))
OFFSET_MINUTES = int(os.environ.get("HEALTHLINE_TIME_OFFSET_MINUTES", "0") or "0")


def now() -> datetime:
    """Current time with optional offset for demo purposes."""
    return datetime.now() + timedelta(minutes=OFFSET_MINUTES)


def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = _dict_factory
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()
        conn.close()


def init_db():
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS patient (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                notes TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS medication (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                dose TEXT NOT NULL,
                frequency_hours INTEGER NOT NULL,
                notes TEXT,
                end_time TEXT,
                start_time TEXT,
                FOREIGN KEY(patient_id) REFERENCES patient(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dose_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medication_id INTEGER NOT NULL,
                scheduled_time TEXT NOT NULL,
                taken INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(medication_id) REFERENCES medication(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fall_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                location TEXT NOT NULL,
                note TEXT,
                FOREIGN KEY(patient_id) REFERENCES patient(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                actor_role TEXT,
                meta_json TEXT
            )
            """
        )
        _ensure_medication_columns(cur)
        _ensure_patient_columns(cur)
        _ensure_dose_event_columns(cur)


def _ensure_medication_columns(cur):
    cur.execute("PRAGMA table_info(medication)")
    cols = {row["name"] for row in cur.fetchall()}
    if "end_time" not in cols:
        cur.execute("ALTER TABLE medication ADD COLUMN end_time TEXT")
    if "start_time" not in cols:
        cur.execute("ALTER TABLE medication ADD COLUMN start_time TEXT")
    if "active" not in cols:
        cur.execute("ALTER TABLE medication ADD COLUMN active INTEGER NOT NULL DEFAULT 1")


def _ensure_patient_columns(cur):
    cur.execute("PRAGMA table_info(patient)")
    cols = {row["name"] for row in cur.fetchall()}
    to_add = [
        ("date_of_birth", "TEXT"),
        ("diagnosis", "TEXT"),
        ("allergies", "TEXT"),
        ("emergency_contact_name", "TEXT"),
        ("emergency_contact_phone", "TEXT"),
        ("emergency_contact_relation", "TEXT"),
    ]
    for name, col_type in to_add:
        if name not in cols:
            cur.execute(f"ALTER TABLE patient ADD COLUMN {name} {col_type}")


def _ensure_dose_event_columns(cur):
    cur.execute("PRAGMA table_info(dose_event)")
    cols = {row["name"] for row in cur.fetchall()}
    if "skipped" not in cols:
        cur.execute("ALTER TABLE dose_event ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")
    if "note" not in cols:
        cur.execute("ALTER TABLE dose_event ADD COLUMN note TEXT")


def get_adherence_summary(patient_id: int, start_iso: str, end_iso: str, now_iso: str):
    """Return counts for due/future doses in range, with due based on now_iso."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN datetime(de.scheduled_time) < datetime(?) AND de.taken = 1 THEN 1 ELSE 0 END) AS taken_due,
                SUM(CASE WHEN datetime(de.scheduled_time) < datetime(?) AND de.skipped = 1 THEN 1 ELSE 0 END) AS skipped_due,
                SUM(CASE WHEN datetime(de.scheduled_time) < datetime(?) AND de.taken = 0 AND de.skipped = 0 THEN 1 ELSE 0 END) AS overdue_due,
                SUM(CASE WHEN de.taken = 0 AND de.skipped = 0 AND datetime(de.scheduled_time) >= datetime(?) THEN 1 ELSE 0 END) AS pending_future
            FROM dose_event de
            JOIN medication m ON de.medication_id = m.id
            WHERE m.patient_id = ?
              AND m.active = 1
              AND datetime(de.scheduled_time) >= datetime(?)
              AND datetime(de.scheduled_time) < datetime(?)
            """,
            (now_iso, now_iso, now_iso, now_iso, patient_id, start_iso, end_iso),
        )
        row = cur.fetchone() or {}
        return {
            "taken_due": row.get("taken_due", 0) or 0,
            "skipped_due": row.get("skipped_due", 0) or 0,
            "overdue_due": row.get("overdue_due", 0) or 0,
            "pending_future": row.get("pending_future", 0) or 0,
        }


def get_overdue_count(patient_id: int, now_iso: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM dose_event de
            JOIN medication m ON de.medication_id = m.id
            WHERE m.patient_id = ?
              AND m.active = 1
              AND de.taken = 0 AND de.skipped = 0
              AND datetime(de.scheduled_time) < datetime(?)
            """,
            (patient_id, now_iso),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0


def get_adherence_7d(patient_id: int, now_iso: str):
    start_iso = (datetime.fromisoformat(now_iso) - timedelta(days=7)).isoformat()
    summary = get_adherence_summary(patient_id, start_iso, now_iso, now_iso)
    taken = summary["taken_due"]
    skipped = summary["skipped_due"]
    overdue = summary["overdue_due"]
    denom = taken + skipped + overdue
    percent = None
    if denom > 0:
        percent = round(taken * 100 / denom, 1)
    return taken, skipped, percent


def add_patient(
    name: str,
    notes: str | None,
    date_of_birth: str | None = None,
    diagnosis: str | None = None,
    allergies: str | None = None,
    emergency_contact_name: str | None = None,
    emergency_contact_phone: str | None = None,
    emergency_contact_relation: str | None = None,
):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO patient (name, notes, date_of_birth, diagnosis, allergies,
                                 emergency_contact_name, emergency_contact_phone, emergency_contact_relation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                notes,
                date_of_birth,
                diagnosis,
                allergies,
                emergency_contact_name,
                emergency_contact_phone,
                emergency_contact_relation,
            ),
        )
        return cur.lastrowid


def list_patients():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM patient ORDER BY id DESC")
        return cur.fetchall()


def get_patient(patient_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM patient WHERE id = ?", (patient_id,))
        return cur.fetchone()


def update_patient(
    patient_id: int,
    name: str,
    notes: str | None,
    date_of_birth: str | None = None,
    diagnosis: str | None = None,
    allergies: str | None = None,
    emergency_contact_name: str | None = None,
    emergency_contact_phone: str | None = None,
    emergency_contact_relation: str | None = None,
):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE patient
            SET name = ?, notes = ?, date_of_birth = ?, diagnosis = ?, allergies = ?,
                emergency_contact_name = ?, emergency_contact_phone = ?, emergency_contact_relation = ?
            WHERE id = ?
            """,
            (
                name,
                notes,
                date_of_birth,
                diagnosis,
                allergies,
                emergency_contact_name,
                emergency_contact_phone,
                emergency_contact_relation,
                patient_id,
            ),
        )


def add_medication(
    patient_id: int,
    name: str,
    dose: str,
    frequency_hours: int,
    notes: str | None,
    start_time: datetime,
    end_time: datetime | None = None,
    active: int = 1,
):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO medication (patient_id, name, dose, frequency_hours, notes, end_time, start_time, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patient_id,
                name,
                dose,
                frequency_hours,
                notes,
                end_time.isoformat() if end_time else None,
                start_time.isoformat(),
                active,
            ),
        )
        medication_id = cur.lastrowid
        _insert_dose_event(cur, medication_id, start_time)
        return medication_id


def list_medications_for_patient(patient_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM medication WHERE patient_id = ? ORDER BY id DESC",
            (patient_id,),
        )
        return cur.fetchall()


def get_medication(medication_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM medication WHERE id = ?", (medication_id,))
        return cur.fetchone()


def update_medication(
    medication_id: int,
    name: str,
    dose: str,
    frequency_hours: int,
    notes: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
    active: int | None = None,
):
    with db_cursor() as cur:
        fields = [
            ("name", name),
            ("dose", dose),
            ("frequency_hours", frequency_hours),
            ("notes", notes),
            ("start_time", start_time.isoformat() if start_time else None),
            ("end_time", end_time.isoformat() if end_time else None),
        ]
        if active is not None:
            fields.append(("active", active))
        set_clause = ", ".join(f"{f[0]} = ?" for f in fields)
        values = [f[1] for f in fields]
        values.append(medication_id)
        cur.execute(
            f"UPDATE medication SET {set_clause} WHERE id = ?",
            values,
        )


def calculate_age(dob_iso: str | None):
    if not dob_iso:
        return None
    try:
        dob = datetime.strptime(dob_iso, "%Y-%m-%d")
        today = now().date()
        years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return years if years >= 0 else None
    except Exception:
        return None


def _insert_dose_event(cur, medication_id: int, when: datetime):
    cur.execute(
        "INSERT INTO dose_event (medication_id, scheduled_time, taken) VALUES (?, ?, ?)",
        (medication_id, when.isoformat(), 0),
    )


def _latest_dose_event_for_med(cur, medication_id: int):
    cur.execute(
        "SELECT * FROM dose_event WHERE medication_id = ? ORDER BY scheduled_time DESC LIMIT 1",
        (medication_id,),
    )
    return cur.fetchone()


def ensure_next_dose_event(medication_id: int):
    """Ensure there is at least one upcoming dose event for this medication."""
    med = get_medication(medication_id)
    if not med:
        return
    if not med.get("active", 1):
        return
    end_time_raw = med.get("end_time")
    end_dt = datetime.fromisoformat(end_time_raw) if end_time_raw else None
    start_time_raw = med.get("start_time")
    start_dt = datetime.fromisoformat(start_time_raw) if start_time_raw else None
    with db_cursor() as cur:
        latest = _latest_dose_event_for_med(cur, medication_id)
        if not latest:
            candidate = max(now(), start_dt) if start_dt else now()
            if end_dt and candidate > end_dt:
                return
            _insert_dose_event(cur, medication_id, candidate)
            return
        # If latest is still pending, wait before generating a new one
        if latest.get("taken") == 0 and latest.get("skipped", 0) == 0:
            return
        last_time = datetime.fromisoformat(latest["scheduled_time"])
        next_time = last_time + timedelta(hours=med["frequency_hours"])
        # move forward until next_time is in present/future
        current = now()
        while next_time < current:
            next_time += timedelta(hours=med["frequency_hours"])
        if end_dt and next_time > end_dt:
            return
        cur.execute(
            """
            SELECT COUNT(*) as count FROM dose_event
            WHERE medication_id = ? AND datetime(scheduled_time) >= datetime(?)
              AND (taken = 0 AND skipped = 0)
            """,
            (medication_id, now().isoformat()),
        )
        pending_exists = cur.fetchone()["count"] > 0
        if not pending_exists:
            _insert_dose_event(cur, medication_id, next_time)


def list_upcoming_dose_events(limit: int = 50, patient_name: str | None = None):
    now_ts = now().isoformat()
    filter_name = (patient_name or "").strip()
    clauses = []
    params = []
    if filter_name:
        clauses.append("LOWER(p.name) LIKE LOWER(?)")
        params.append(f"%{filter_name}%")
    with db_cursor() as cur:
        query = """
            SELECT de.*, m.name AS medication_name, m.dose AS medication_dose, m.frequency_hours,
                   p.name AS patient_name
            FROM dose_event de
            JOIN medication m ON de.medication_id = m.id
            JOIN patient p ON m.patient_id = p.id
            WHERE datetime(de.scheduled_time) >= datetime(?, '-1 day')
        """
        if clauses:
            query += " AND " + " AND ".join(clauses)
        query += " ORDER BY de.scheduled_time ASC LIMIT ?"
        cur.execute(query, (now_ts, *params, limit))
        events = cur.fetchall()
    return events


def get_patient_names():
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT name FROM patient ORDER BY name ASC")
        rows = cur.fetchall()
        return [r["name"] for r in rows]


def list_dose_events_for_medication(medication_id: int, limit: int = 10):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM dose_event
            WHERE medication_id = ?
            ORDER BY scheduled_time DESC
            LIMIT ?
            """,
            (medication_id, limit),
        )
        return cur.fetchall()


def mark_dose_taken(event_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM dose_event WHERE id = ?", (event_id,))
        event = cur.fetchone()
        if not event:
            return
        cur.execute("UPDATE dose_event SET taken = 1, skipped = 0 WHERE id = ?", (event_id,))
        med_id = event["medication_id"]
    ensure_next_dose_event(med_id)


def mark_dose_skipped(event_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM dose_event WHERE id = ?", (event_id,))
        event = cur.fetchone()
        if not event:
            return
        cur.execute("UPDATE dose_event SET skipped = 1, taken = 0 WHERE id = ?", (event_id,))
        med_id = event["medication_id"]
    ensure_next_dose_event(med_id)


def undo_dose_event(event_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM dose_event WHERE id = ?", (event_id,))
        event = cur.fetchone()
        if not event:
            return
        cur.execute("UPDATE dose_event SET skipped = 0, taken = 0 WHERE id = ?", (event_id,))
        med_id = event["medication_id"]
    ensure_next_dose_event(med_id)


def save_dose_note(event_id: int, note: str | None):
    with db_cursor() as cur:
        cur.execute("UPDATE dose_event SET note = ? WHERE id = ?", (note, event_id))


def list_overdue_pending_events(patient_id: int, now_iso: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT de.*, m.name AS medication_name, m.active AS medication_active
            FROM dose_event de
            JOIN medication m ON de.medication_id = m.id
            WHERE m.patient_id = ?
              AND m.active = 1
              AND de.taken = 0 AND de.skipped = 0
              AND datetime(de.scheduled_time) < datetime(?)
            ORDER BY de.scheduled_time ASC
            """,
            (patient_id, now_iso),
        )
        return cur.fetchall()


def log_audit(action: str, entity_type: str, entity_id: int | None, actor_role: str | None, meta: dict | None = None):
    ts = now().isoformat()
    payload = json.dumps(meta) if meta else None
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (ts, action, entity_type, entity_id, actor_role, meta_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, action, entity_type, entity_id, actor_role, payload),
        )


def delete_patient(patient_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT id FROM medication WHERE patient_id = ?", (patient_id,))
        meds = cur.fetchall()
        med_ids = [m["id"] for m in meds]
        if med_ids:
            cur.execute(
                f"DELETE FROM dose_event WHERE medication_id IN ({','.join('?' for _ in med_ids)})",
                med_ids,
            )
        cur.execute("DELETE FROM medication WHERE patient_id = ?", (patient_id,))
        cur.execute("DELETE FROM patient WHERE id = ?", (patient_id,))


def reset_future_events(medication_id: int, seed_time: datetime | None = None):
    """Delete pending future events and create one seed event respecting end_time."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM medication WHERE id = ?", (medication_id,))
        med = cur.fetchone()
        if not med:
            return
        now_dt = now()
        seed = seed_time or now_dt
        target_time = seed if seed > now_dt else now_dt
        cur.execute(
            """
            DELETE FROM dose_event
            WHERE medication_id = ?
              AND taken = 0
              AND datetime(scheduled_time) >= datetime(?)
            """,
            (medication_id, now_dt.isoformat()),
        )
        end_raw = med.get("end_time")
        end_dt = datetime.fromisoformat(end_raw) if end_raw else None
        if end_dt and target_time > end_dt:
            return
        _insert_dose_event(cur, medication_id, target_time)


def delete_pending_events_for_medication(medication_id: int):
    with db_cursor() as cur:
        cur.execute(
            """
            DELETE FROM dose_event
            WHERE medication_id = ?
              AND taken = 0
            """,
            (medication_id,),
        )


def add_fall_event(patient_id: int, occurred_at_dt: datetime, location: str, note: str | None):
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO fall_event (patient_id, occurred_at, location, note) VALUES (?, ?, ?, ?)",
            (patient_id, occurred_at_dt.isoformat(), location, note),
        )


def list_fall_events(patient_id: int, limit: int = 50):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM fall_event
            WHERE patient_id = ?
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (patient_id, limit),
        )
        return cur.fetchall()


def count_falls_last_90_days(patient_id: int, now_iso: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM fall_event
            WHERE patient_id = ?
              AND datetime(occurred_at) >= datetime(?, '-90 days')
            """,
            (patient_id, now_iso),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0

def reset_all():
    with db_cursor() as cur:
        cur.execute("DELETE FROM dose_event")
        cur.execute("DELETE FROM medication")
        cur.execute("DELETE FROM patient")


def seed_demo():
    reset_all()
    base_now = now()
    # Helper to build DOB safely between 65-95 years old
    def dob_for_age(age_years: int) -> str:
        target_year = base_now.year - age_years
        # use mid-year to avoid month/day edge cases
        return f"{target_year}-06-15"

    patients = [
        {
            "name": "Maria Alvarez",
            "notes": "Seguimiento HTA y DM2",
            "date_of_birth": dob_for_age(78),
            "diagnosis": "Hipertensión y diabetes tipo 2",
            "allergies": "Penicilina",
            "ec_name": "Laura Alvarez",
            "ec_phone": "555-8101",
            "ec_relation": "Hija",
            "meds": [
                {"name": "Enalapril", "dose": "10mg", "freq": 12, "notes": "HTA", "start_offset": -10, "end_offset": None, "active": 1},
                {"name": "Metformina", "dose": "850mg", "freq": 12, "notes": "DM2", "start_offset": -6, "end_offset": None, "active": 1},
                {"name": "Simvastatina", "dose": "20mg", "freq": 24, "notes": "Lípidos", "start_offset": -20, "end_offset": None, "active": 1},
            ],
        },
        {
            "name": "Jorge Ramirez",
            "notes": "Deterioro cognitivo",
            "date_of_birth": dob_for_age(84),
            "diagnosis": "Alzheimer",
            "allergies": "Sin alergias conocidas",
            "ec_name": "Sofia Ramirez",
            "ec_phone": "555-8202",
            "ec_relation": "Hija",
            "meds": [
                {"name": "Donepezilo", "dose": "10mg", "freq": 24, "notes": "Demencia", "start_offset": -24, "end_offset": None, "active": 1},
                {"name": "Memantina", "dose": "10mg", "freq": 12, "notes": "Demencia", "start_offset": -8, "end_offset": None, "active": 1},
                {"name": "Quetiapina", "dose": "25mg", "freq": 24, "notes": "Conducta nocturna", "start_offset": -2, "end_offset": None, "active": 1},
            ],
        },
        {
            "name": "Carmen Soto",
            "notes": "EPOC + HTA",
            "date_of_birth": dob_for_age(90),
            "diagnosis": "EPOC e hipertensión",
            "allergies": "AINEs",
            "ec_name": "Diego Soto",
            "ec_phone": "555-8303",
            "ec_relation": "Hijo",
            "meds": [
                {"name": "Salmeterol", "dose": "1 inhalación", "freq": 12, "notes": "EPOC", "start_offset": -5, "end_offset": None, "active": 1},
                {"name": "Tiotropio", "dose": "1 inhalación", "freq": 24, "notes": "EPOC", "start_offset": -15, "end_offset": None, "active": 1},
                {"name": "Amoxicilina", "dose": "500mg", "freq": 8, "notes": "Exacerbación, antibiótico", "start_offset": -12, "end_offset": 24, "active": 1},
            ],
        },
        {
            "name": "Luis Herrera",
            "notes": "ICC y ERC",
            "date_of_birth": dob_for_age(70),
            "diagnosis": "Insuficiencia cardiaca y renal",
            "allergies": "Sulfas",
            "ec_name": "Carmen Herrera",
            "ec_phone": "555-8404",
            "ec_relation": "Esposa",
            "meds": [
                {"name": "Furosemida", "dose": "40mg", "freq": 24, "notes": "ICC", "start_offset": -26, "end_offset": None, "active": 1},
                {"name": "Carvedilol", "dose": "12.5mg", "freq": 12, "notes": "ICC", "start_offset": -4, "end_offset": None, "active": 1},
                {"name": "Calcio", "dose": "600mg", "freq": 24, "notes": "Suplemento", "start_offset": 2, "end_offset": None, "active": 1},
            ],
        },
        {
            "name": "Elena Chavez",
            "notes": "Dolor crónico y ánimo",
            "date_of_birth": dob_for_age(82),
            "diagnosis": "Osteoartritis y depresión geriátrica",
            "allergies": "Látex",
            "ec_name": "Mario Chavez",
            "ec_phone": "555-8505",
            "ec_relation": "Hijo",
            "meds": [
                {"name": "Paracetamol", "dose": "500mg", "freq": 6, "notes": "Dolor", "start_offset": -3, "end_offset": None, "active": 1},
                {"name": "Duloxetina", "dose": "60mg", "freq": 24, "notes": "Ánimo/dolor", "start_offset": -22, "end_offset": None, "active": 1},
                {"name": "Naproxeno", "dose": "250mg", "freq": 12, "notes": "Dolor articular (pausado)", "start_offset": -10, "end_offset": None, "active": 0},
            ],
        },
        {
            "name": "Rosa Medina",
            "notes": "DM2 con neuropatía",
            "date_of_birth": dob_for_age(88),
            "diagnosis": "Diabetes tipo 2 con neuropatía",
            "allergies": "Sin alergias conocidas",
            "ec_name": "Andres Medina",
            "ec_phone": "555-8606",
            "ec_relation": "Nieto",
            "meds": [
                {"name": "Insulina NPH", "dose": "18U", "freq": 12, "notes": "DM2", "start_offset": -7, "end_offset": None, "active": 1},
                {"name": "Pregabalina", "dose": "75mg", "freq": 12, "notes": "Neuropatía", "start_offset": -1, "end_offset": None, "active": 1},
            ],
        },
    ]

    for p in patients:
        pid = add_patient(
            p["name"],
            p["notes"],
            date_of_birth=p.get("date_of_birth"),
            diagnosis=p.get("diagnosis"),
            allergies=p.get("allergies"),
            emergency_contact_name=p.get("ec_name"),
            emergency_contact_phone=p.get("ec_phone"),
            emergency_contact_relation=p.get("ec_relation"),
        )
        for m in p["meds"]:
            start_time = base_now + timedelta(hours=m["start_offset"])
            end_time = base_now + timedelta(hours=m["end_offset"]) if m["end_offset"] is not None else None
            mid = add_medication(
                pid,
                m["name"],
                m["dose"],
                m["freq"],
                m["notes"],
                start_time,
                end_time=end_time,
                active=m["active"],
            )
            if m["active"]:
                for _ in range(2):
                    ensure_next_dose_event(mid)
            else:
                # limpiar pendientes del pausado
                delete_pending_events_for_medication(mid)


def list_patient_events_for_day(patient_id: int, day_start_iso: str, day_end_iso: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT de.*, m.name AS medication_name, m.dose AS medication_dose, m.frequency_hours,
                   p.name AS patient_name
            FROM dose_event de
            JOIN medication m ON de.medication_id = m.id
            JOIN patient p ON m.patient_id = p.id
            WHERE p.id = ?
              AND datetime(de.scheduled_time) >= datetime(?)
              AND datetime(de.scheduled_time) < datetime(?)
            ORDER BY de.scheduled_time ASC
            """,
            (patient_id, day_start_iso, day_end_iso),
        )
        return cur.fetchall()
