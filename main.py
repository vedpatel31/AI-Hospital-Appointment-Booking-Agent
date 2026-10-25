"""
Hospital Appointment Booking -- FastAPI + PostgreSQL + Qwen2.5-0.5B-Instruct (local, offline).

Run:  python main.py
User page : http://localhost:8000/
Admin page: http://localhost:8000/admin
"""
import os, re, json, secrets, threading, time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

# =====================================================================
# FILE MAP - search for these section names to jump around:
#   CONFIG ................ hospital name, DB login, admin password, AI model folder, time slots
#   DB helpers ............ PostgreSQL connection pool, query helpers, table definitions, seed data
#   LLM ................... loads the local Qwen AI model (see note: not used by the chat flow now)
#   Hospital knowledge .... helpers that read doctors / departments / free slots from the database
#   Extract helpers ....... pull phone, email, date, time, doctor, department out of the user's text
#   BOOKING STATE MACHINE . the chatbot brain: decides what to ask next and builds every BOT REPLY
#   FastAPI app ........... web routes: user page, admin page, /api/chat
#   Admin API ............. admin login + manage doctors / departments / appointments
# =====================================================================
# CONFIG
# Hospital name shown inside bot replies. Can be overridden with an environment variable.
HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "City Care Hospital")
# DATABASE SETTINGS: PostgreSQL connection details (host, port, database name, user, password).
# Each value is read from an environment variable first; the second argument is the default.
# NOTE: the default DB password is written directly in the code - prefer setting DB_PASSWORD
# as an environment variable and removing it from here before sharing this file.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")   # reads the .env file next to main.py

DB_HOST        = os.environ["DB_HOST"]
DB_PORT        = int(os.environ["DB_PORT"])
DB_NAME        = os.environ["DB_NAME"]
DB_USER        = os.environ["DB_USER"]
DB_PASSWORD    = os.environ["DB_PASSWORD"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

MODEL_DIR = os.environ["MODEL_DIR"]
os.environ["HF_HOME"] = os.path.join(MODEL_DIR, "cache")
MODEL_ID  = os.environ["MODEL_ID"]

# BOOKING RULES: the fixed daily appointment time slots (24h format) every doctor offers,
# and how many days ahead a patient is allowed to book (MAX_DAYS).
SLOTS    = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
MAX_DAYS = 60

# Third-party libraries: psycopg2 = PostgreSQL driver (+ connection pool), dateutil = date/time text parsing,
# FastAPI = web framework, pydantic = validates the JSON sent to the API.
import psycopg2, psycopg2.errors
from psycopg2.extras import RealDictCursor
from psycopg2.pool  import ThreadedConnectionPool
from dateutil       import parser as dateparser
from fastapi        import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic       import BaseModel, Field

# BASE_DIR = folder of this file (used to find static/index.html and static/admin.html).
# pool = PostgreSQL connection pool, created later inside init_db().
BASE_DIR = Path(__file__).parent
pool     = None

# DB helpers
# DATABASE: connects to the default 'postgres' database and creates 'hospital_db' if it does not exist yet.
def ensure_database():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                            password=DB_PASSWORD, dbname="postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{DB_NAME}"')
    conn.close()

# DATABASE: gives a cursor from the connection pool. Commits if the block succeeds,
# rolls back on any error, and always returns the connection to the pool.
@contextmanager
def db():
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        pool.putconn(conn)

# DATABASE: the main query helper used everywhere.  q("SQL", params) -> list of rows (dicts);
# one=True -> just a single row; statements with no result (INSERT/UPDATE without RETURNING) -> None.
def q(sql, params=None, one=False):
    with db() as cur:
        cur.execute(sql, params)
        if cur.description is None: return None
        return cur.fetchone() if one else cur.fetchall()

# DATABASE TABLES (created automatically on startup, only if they do not already exist):
#   departments           - list of hospital departments
#   doctors               - doctor profile + 'available' flag, linked to a department
#   appointments          - every booking (patient, phone, email, doctor, date, time slot, status)
#   conversation_sessions - saves each chat's state as JSON so a user can refresh the page and continue
# Unique indexes: booking_key stops the same booking being saved twice, and uq_doctor_slot
# stops two 'booked' appointments for the same doctor + date + time (prevents double booking).
SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id   SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS doctors (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(100) NOT NULL,
    department_id    INT  NOT NULL REFERENCES departments(id),
    qualification    VARCHAR(150) NOT NULL DEFAULT '',
    experience_years INT  NOT NULL DEFAULT 0,
    details          TEXT NOT NULL DEFAULT '',
    available        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS appointments (
    id               SERIAL PRIMARY KEY,
    patient_name     VARCHAR(100) NOT NULL,
    phone            VARCHAR(15)  NOT NULL,
    email            VARCHAR(150) NOT NULL,
    doctor_id        INT  NOT NULL REFERENCES doctors(id),
    appointment_date DATE NOT NULL,
    time_slot        VARCHAR(5)   NOT NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'booked',
    booking_key      VARCHAR(64),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS booking_key VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_booking_key ON appointments (booking_key);
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_doctor_slot
    ON appointments (doctor_id, appointment_date, time_slot)
    WHERE status = 'booked';
"""

# DATABASE: sample data inserted ONLY on the very first run (when the departments table is empty).
SEED_DEPARTMENTS = ["General Medicine","Cardiology","Orthopedics",
                    "Pediatrics","Dermatology","ENT","Gynecology"]
# Sample doctors: (name, department, qualification, years of experience, what they treat).
SEED_DOCTORS = [
    ("Dr. Rajesh Shah",  "General Medicine","MBBS, MD",              15,"Fever, infections, diabetes and general health checkups."),
    ("Dr. Meera Patel",  "Cardiology",      "MBBS, DM Cardiology",   12,"Heart disease, blood pressure and ECG."),
    ("Dr. Arjun Desai",  "Orthopedics",     "MBBS, MS Ortho",        10,"Bone, joint and sports injuries."),
    ("Dr. Neha Joshi",   "Pediatrics",      "MBBS, DCH",              8,"Child health, vaccination and growth."),
    ("Dr. Karan Mehta",  "Dermatology",     "MBBS, MD Dermatology",   9,"Skin, hair and allergy problems."),
    ("Dr. Sana Khan",    "ENT",             "MBBS, MS ENT",           7,"Ear, nose and throat."),
    ("Dr. Priya Nair",   "Gynecology",      "MBBS, MS OBG",          14,"Women's health and pregnancy care."),
]

# DATABASE: runs once at server start - creates the DB if needed, opens the connection pool,
# creates the tables, and inserts the sample departments/doctors if the tables are empty.
def init_db():
    global pool
    ensure_database()
    pool = ThreadedConnectionPool(1, 10, host=DB_HOST, port=DB_PORT,
                                  user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME)
    with db() as cur:
        cur.execute(SCHEMA)
        cur.execute("SELECT count(*) AS n FROM departments")
        if cur.fetchone()["n"] == 0:
            for name in SEED_DEPARTMENTS:
                cur.execute("INSERT INTO departments (name) VALUES (%s)", (name,))
            for name, dept, qual, exp, details in SEED_DOCTORS:
                cur.execute(
                    """INSERT INTO doctors (name, department_id, qualification, experience_years, details)
                       SELECT %s, id, %s, %s, %s FROM departments WHERE name=%s""",
                    (name, qual, exp, details, dept))

# LLM
# AI MODEL STATE: one shared dictionary tracking whether the model is ready, its status
# (starting / downloading / loading / ready / error), tokenizer, model, etc.
# /api/llm-status reads this so the website header can show the status dot.
# llm_lock makes sure only one thread uses the model at a time.
llm = {"ready": False, "error": None, "tok": None, "model": None,
       "torch": None, "device": "cpu", "status": "starting"}
llm_lock = threading.Lock()

# Full path of the model folder inside MODEL_DIR.
def _model_path():
    return os.path.join(MODEL_DIR, MODEL_ID.split("/")[-1])

# Checks that every required model file is present on disk (config, tokenizer, weights / shards).
def _complete(path):
    def has(f): return os.path.exists(os.path.join(path, f))
    if not has("config.json"): return False
    if not (has("tokenizer.json") or (has("vocab.json") and has("merges.txt"))): return False
    idx = os.path.join(path, "model.safetensors.index.json")
    if os.path.exists(idx):
        try:
            with open(idx, encoding="utf-8") as f:
                shards = set(json.load(f)["weight_map"].values())
            return all(has(s) for s in shards)
        except Exception: return False
    return has("model.safetensors") or has("pytorch_model.bin")

# AI MODEL: runs in a background thread at startup. Downloads the model if missing, loads it on CPU,
# and updates the llm dictionary so the website knows when it is ready (or if it failed).
def load_llm():
    try:
        os.makedirs(MODEL_DIR, exist_ok=True)
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        path = _model_path()
        if not (os.path.isdir(path) and _complete(path)):
            from huggingface_hub import snapshot_download
            llm["status"] = "downloading"
            print(f"[model] Downloading {MODEL_ID} into {path} ...")
            snapshot_download(MODEL_ID, local_dir=path)
            if not _complete(path):
                raise RuntimeError("Download incomplete -- restart to retry.")
            print("[model] Download complete.")
        llm["status"] = "loading"
        print(f"[model] Loading {MODEL_ID} ...")
        tok   = AutoTokenizer.from_pretrained(path)
        model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.float32)
        model = model.to("cpu").eval()
        llm.update(tok=tok, model=model, torch=torch, device="cpu", ready=True, status="ready")
        print("[model] Ready on CPU.")
    except BaseException as e:
        llm["error"]  = f"{type(e).__name__}: {e}"
        llm["status"] = "error"
        print("[model] Load failed:", llm["error"])

# Hospital knowledge
# Keywords that map to a department (e.g. "heart" or "bp" -> Cardiology). Used to understand
# what the patient types, and to suggest a department from their symptoms/words.
DEPT_ALIASES = {
    "cardiology":       ["cardiologist","cardiac","heart","bp","blood pressure"],
    "orthopedics":      ["orthopedic","orthopaedic","bone","joint","fracture","knee","back pain"],
    "pediatrics":       ["pediatric","pediatrician","child","kids","baby","infant"],
    "dermatology":      ["dermatologist","skin","hair","rash","acne","allergy"],
    "gynecology":       ["gynecologist","gynaecologist","gynaecology","women","pregnancy","period"],
    "general medicine": ["general physician","physician","fever","cold","diabetes","checkup"],
    "ent":              ["ent","ear","nose","throat","hearing","sinus"],
}

# EMERGENCY CHECK: if the user's message matches any of these words (chest pain, stroke, ...),
# chat_turn() replies with an emergency warning instead of continuing the booking.
EMERGENCY_RE = re.compile(
    r"chest pain|heart attack|can'?t breathe|cannot breathe|difficulty breathing|"
    r"unconscious|seizure|stroke|heavy bleeding|poison|overdose|medical emergency|"
    r"ambulance|suicid", re.I)

# DATABASE: reads every doctor with their department name (available or not).
def all_doctors():
    return q("""SELECT d.id, d.name, d.qualification, d.experience_years,
                       d.details, d.available, dp.name AS department
                FROM doctors d JOIN departments dp ON dp.id = d.department_id
                ORDER BY dp.name, d.name""")

# Display name: adds the "Dr." prefix if the stored name doesn't already start with it.
def dn(name):
    return name if name.lower().startswith("dr") else f"Dr. {name}"

# Converts a slot like "14:00" into "02:00 PM" for display in bot replies.
def fmt_slot(s):
    return datetime.strptime(s, "%H:%M").strftime("%I:%M %p")

# Builds a text list of all doctors. Used as the 'knowledge' given to the LLM in llm_answer().
def hospital_context():
    docs  = all_doctors()
    lines = []
    for d in docs:
        status = "available" if d["available"] else "NOT available"
        lines.append(
            f"  - {dn(d['name'])} | Dept: {d['department']} | {d['qualification']} "
            f"| {d['experience_years']} yrs exp | Treats: {d['details']} | {status}")
    return "\n".join(lines)

# DATABASE: returns the time slots still free for a doctor on a date.
# Skips slots already 'booked' in the appointments table, slots that have already passed today,
# and returns nothing if the doctor is marked unavailable.
def free_slots(doctor_id, d):
    doctor = q("SELECT available FROM doctors WHERE id=%s", (doctor_id,), one=True)
    if not doctor or not doctor["available"]:
        return []
    rows  = q("""SELECT time_slot FROM appointments
                 WHERE doctor_id=%s AND appointment_date=%s AND status='booked'""", (doctor_id, d))
    taken = {r["time_slot"] for r in rows}
    now   = datetime.now().strftime("%H:%M")
    return [s for s in SLOTS if s not in taken and (d != date.today() or s > now)]

# DATABASE: departments that have at least one available doctor (used for the department choices).
def list_departments():
    return q("""SELECT d.id, d.name FROM departments d
                WHERE EXISTS (SELECT 1 FROM doctors x WHERE x.department_id=d.id AND x.available)
                ORDER BY d.name""")

# DATABASE: available doctors of one department (used for the doctor choices).
def list_doctors(dept_id):
    return q("""SELECT id, name, qualification, experience_years FROM doctors
                WHERE department_id=%s AND available ORDER BY name""", (dept_id,))

# Extract helpers
# EXTRACT HELPERS: turn free text typed by the patient into clean values.
# Regex patterns: Indian 10-digit mobile number (starts with 6-9, optional +91) and email address.
PHONE_RE = re.compile(r"\b(?:\+?91[-\s]?)?([6-9]\d{9})\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Returns the 10-digit phone number found in the text, or None.
def extract_phone(text):
    m = PHONE_RE.search(text)
    return m.group(1) if m else None

# Returns the email address found in the text (lower-cased), or None.
def extract_email(text):
    m = EMAIL_RE.search(text)
    return m.group(0).lower() if m else None

# Understands "today", "tomorrow", "day after" and typed dates like 26 Oct 2026 or 2026-10-26.
# Returns a date object or None.
def extract_date(text):
    t = text.strip().lower()
    today = date.today()
    if re.search(r"\btoday\b", t):   return today
    if re.search(r"\btomorrow\b", t): return today + timedelta(days=1)
    if re.search(r"\bday after\b", t): return today + timedelta(days=2)
    for pat in (r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", r"\d{1,2}[-/]\d{1,2}[-/]\d{4}",
                r"\d{1,2}\s+\w+\s+\d{4}", r"\w+\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*\d{4}"):
        m = re.search(pat, t)
        if m:
            try:
                return dateparser.parse(m.group(), dayfirst=True,
                                        default=datetime(today.year, today.month, today.day)).date()
            except Exception: pass
    return None

# Understands a slot by its list number ("2") or by a time ("10am", "14:00"). Returns "HH:MM" or None.
def extract_slot(text, slots):
    t = text.strip().lower()
    if t.isdigit() and 1 <= int(t) <= len(slots):
        return slots[int(t)-1]
    try:
        hhmm = dateparser.parse(t).strftime("%H:%M")
        return hhmm if hhmm in slots else None
    except Exception: return None

# Regex that strips filler words like "my name is", "I am", "call me" before reading the name.
NAME_NOISE = re.compile(
    r"^(?:(?:hi|hello|hey|good\s+\w+)[,!.\s]*)?(?:my\s+(?:full\s+)?name\s+is|"
    r"my\s+name'?s?|name\s+is|this\s+is|i\s+am|i'm|im|it'?s|myself|call\s+me)\s+", re.I)

# Cleans and validates a patient name (letters only). NOTE: not called anywhere right now -
# the name step uses its own similar logic inside validate_step().
def extract_name(text):
    cleaned = NAME_NOISE.sub("", text.strip()).strip(" .!,")
    if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", cleaned):
        return " ".join(cleaned.title().split())
    return None

# Matches typed text to one doctor from the given list (exact name, or a unique partial name).
def match_doctor(text, docs):
    t = text.strip().lower()
    t = re.sub(r"^dr\.?\s*", "", t)
    if t.isdigit() and 1 <= int(t) <= len(docs):      # NEW
        return docs[int(t)-1]                          # NEW
    for d in docs:
        name_clean = re.sub(r"^dr\.?\s*", "", d["name"].lower())
        if t == name_clean: return d
    if len(t) >= 3:
        found = [d for d in docs if t in d["name"].lower()]
        if len(found) == 1: return found[0]
    return None

# Matches typed text to a department by list number, department name, or an alias keyword.
def match_department(text, depts):
    t = text.strip().lower()
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(depts): return depts[idx]
    for dept in depts:
        names = [dept["name"].lower()] + DEPT_ALIASES.get(dept["name"].lower(), [])
        if any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in names):
            return dept
    return None


# =============================================================================
# LLM — used ONLY for answering general info questions (never drives booking)
# =============================================================================

# LLM ANSWER (AI reply): asks the local Qwen model a general question, giving it the doctor list as
# its only knowledge. Returns the generated text or None.
# NOTE: this function is defined but NOT called anywhere in the chat flow at the moment;
# all current replies are scripted (see quick_doctor_answer and the booking state machine).
def llm_answer(question: str):
    """Use local Qwen to answer a general hospital/doctor question. Returns str or None."""
    if not llm["ready"]:
        return None
    try:
        ctx = hospital_context()
        # System prompt = the instructions/rules given to the AI model (role, length limit, use only the doctor list).
        system = (
            f"You are Aria, the friendly front-desk assistant at {HOSPITAL_NAME}. "
            "Answer in plain text, at most 3 short sentences. "
            "Use ONLY the doctor list below. Never invent names, fees, or timings. "
            "For possible emergencies, advise contacting local emergency services or "
            "going to the nearest emergency department immediately. Do not invent an "
            "emergency phone number.\n\n"
            f"Doctor directory:\n{ctx}"
        )
        tok, model, torch = llm["tok"], llm["model"], llm["torch"]
        msgs = [
            {"role": "system", "content": system},
            {"role": "user",   "content": question},
        ]
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = tok(prompt, return_tensors="pt").to("cpu")
        # Generates the answer text (max 120 new tokens) - locked so only one request uses the model at a time.
        with llm_lock, torch.no_grad():
            out = model.generate(
                **inputs, max_new_tokens=120, do_sample=True,
                temperature=0.6, top_p=0.85, repetition_penalty=1.1,
                pad_token_id=tok.eos_token_id)
        text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        return text or None
    except Exception as e:
        print("[llm] Error:", repr(e))
        return None


# BOT REPLY (info questions): answers directory questions straight from the DATABASE, no AI involved:
# list of departments, list of doctors, details of a named doctor, or doctors of a department.
# Returns None if the question isn't recognised.
def quick_doctor_answer(text: str):
    """Scripted fast answer from DB — no LLM needed. Returns str or None."""
    tl = text.lower()
    docs = all_doctors()

    # List configured departments from the database.
    if re.search(r"\b(departments?|specialit|specialisation)\b", tl):
        depts = [d["name"] for d in q("SELECT name FROM departments ORDER BY name")]
        if not depts:
            return "There are no departments configured at the moment."
        return "Our configured departments are: " + ", ".join(depts) + "."

    # List all doctors
    if re.search(r"\b(doctors?|specialists?|doctor names?)\b", tl) and re.search(
            r"\b(list|all|available|which|who|show|names?|give|other|another)\b", tl):
        if not docs:
            return "There are no doctors configured at the moment."
        lines = "\n".join(
            f"  - {dn(d['name'])} | {d['department']} "
            f"| {d['qualification']} | {d['experience_years']} yrs exp "
            f"| {'Available for booking' if d['available'] else 'Not currently available'}"
            for d in docs)
        return ("Here are the doctors in the configured directory. A time slot still needs "
                "to be checked for your chosen date:\n" + lines)

    # Named doctor lookup
    words = set(re.findall(r"[a-z]+", tl))
    for d in docs:
        parts = [p for p in re.findall(r"[a-z]+", re.sub(r"^dr\.?\s*", "", d["name"].lower()))
                 if len(p) >= 3]
        if any(p in words for p in parts):
            status = "available for booking" if d["available"] else "NOT currently available"
            return (f"{dn(d['name'])} ({d['department']}) — {d['qualification']}, "
                    f"{d['experience_years']} years experience. Treats: {d['details']}. "
                    f"Currently {status}.")

    # Department keyword lookup
    for dept in q("SELECT id, name FROM departments ORDER BY name"):
        dept_key = dept["name"].lower()
        all_names = [dept_key] + DEPT_ALIASES.get(dept_key, [])
        if any(re.search(r"\b" + re.escape(n) + r"\b", tl) for n in all_names):
            matching = [d for d in docs if d["department"].lower() == dept_key]
            if not matching:
                return f"There are no doctors configured in {dept['name']}."
            lines = "\n".join(
                f"  - {dn(d['name'])} | {d['qualification']} | {d['experience_years']} yrs exp "
                f"| {'Available for booking' if d['available'] else 'Not currently available'}"
                for d in matching)
            return f"Doctors in {dept['name']}:\n" + lines

    return None


# =============================================================================
# BOOKING STATE MACHINE
# Python drives ALL booking steps. LLM is NEVER used to confirm or book.
# =============================================================================

# CHAT SESSIONS: in-memory cache of every user's conversation state, keyed by session id (from the browser).
# session_locks = one lock per session so two messages from the same user are never processed at once.
# The same state is also saved to the conversation_sessions DB table (see save_session / load_session).
sessions: dict = {}
session_locks: dict = {}
session_locks_guard = threading.Lock()

# BOOKING_STEPS is not used anywhere (STEP_ORDER, defined further below, is the one actually used).
# RESET_WORDS = typing one of these restarts the booking from the beginning.
BOOKING_STEPS = ["name", "phone", "email", "dept", "doctor", "date", "slot"]
RESET_WORDS   = {"restart", "start over", "reset"}

# Words that count as "yes" / "no" when the user confirms or cancels a booking.
YES_SET = {
    "yes","y","yeah","yep","yup","confirm","ok","okay","sure","book","book it",
    "go ahead","please book","do it","proceed","book now","confirmed",
    "absolutely","of course","please confirm","please do","haan","ha",
}
NO_SET = {"no","n","nope","cancel","stop","abort","nahi","na"}


# True if the message means yes / confirm.
def is_yes(t: str) -> bool:
    t = t.strip().lower()
    if t in YES_SET:
        return True
    return any(t.startswith(w) for w in (
        "yes", "yeah", "ok ", "sure", "book", "go ahead", "please", "confirm"))


# True if the message means no / cancel.
def is_no(t: str) -> bool:
    t = t.strip().lower()
    if t in NO_SET:
        return True
    return any(t.startswith(w) for w in ("no ", "nope", "cancel", "stop"))


# True if the message looks like a question about doctors/departments (so it is answered
# instead of being treated as the answer to the current booking question).
def is_info_question(text: str) -> bool:
    """True if the message looks like a general info query, not a booking field answer."""
    tl = text.lower()
    asks_about_directory = re.search(
        r"\b(doctors?|dr|specialists?|departments?|specialit|qualification|experience)\b", tl
    ) and re.search(
        r"\b(list|show|give|who|which|names?|details|about|work|available|tell|do you have)\b", tl
    )
    if asks_about_directory:
        return True
    return bool(re.search(r"\b(what|where|when|who|which|how|do you have|can you tell)\b", tl))


# Starting state of a brand-new chat: state = idle/booking/confirm/done, step = which question is
# being asked, data = the booking details collected so far, history = last messages.
def new_session() -> dict:
    return {
        "step":    "name",
        "state":   "idle",
        "data":    {},
        "history": [],
    }


# Returns the lock belonging to one session id (creates it the first time).
def _session_lock(sid: str):
    with session_locks_guard:
        return session_locks.setdefault(sid, threading.RLock())


# DATABASE: loads a saved conversation from conversation_sessions (used when the server restarted
# or the page was refreshed). Converts the saved date text back into a date object.
def load_session(sid: str):
    row = q("SELECT payload FROM conversation_sessions WHERE session_id=%s", (sid,), one=True)
    if not row:
        return None
    state = row["payload"]
    if isinstance(state, str):
        state = json.loads(state)
    if isinstance(state.get("data", {}).get("apt_date"), str):
        state["data"]["apt_date"] = date.fromisoformat(state["data"]["apt_date"])
    return state


# DATABASE: saves/updates the conversation state as JSON in conversation_sessions
# (INSERT ... ON CONFLICT UPDATE = create if new, update if it already exists).
def save_session(sid: str, state: dict):
    payload = dict(state)
    payload["data"] = dict(state["data"])
    if isinstance(payload["data"].get("apt_date"), date):
        payload["data"]["apt_date"] = payload["data"]["apt_date"].isoformat()
    q(
        """INSERT INTO conversation_sessions (session_id, payload)
           VALUES (%s, %s::jsonb)
           ON CONFLICT (session_id) DO UPDATE
           SET payload=EXCLUDED.payload, updated_at=now()""",
        (sid, json.dumps(payload)),
    )


# ── friendly step prompts ────────────────────────────────────────────────────

# BOT REPLY (questions): the text the bot asks for each booking step - name, phone, email,
# department, doctor, date, time slot. Returns (reply_text, options). The 'options' list becomes
# the clickable chip buttons on the website (e.g. department names, "Today"/"Tomorrow", time slots).
def ask_for(step: str, d: dict) -> tuple:
    """Returns (reply_text: str, options: list)."""
    if step == "name":
        return "Could I get your full name, please?", []

    if step == "phone":
        return "What is your 10-digit mobile number?", []

    if step == "email":
        return "And your email address?", []

    if step == "dept":
        depts = list_departments()
        if not depts:
            return "Sorry, no departments are available right now. Please try later.", []
        lines = "\n".join(f"  {i}. {x['name']}" for i, x in enumerate(depts, 1))
        return (
            f"Which department would you like to visit? Here are our options:\n{lines}",
            [x["name"] for x in depts],
        )

    if step == "doctor":
        docs = list_doctors(d["dept_id"])
        if not docs:
            return f"Sorry, no doctors are available in {d['dept_name']} right now.", []
        lines = "\n".join(
            f"  {i}. {dn(x['name'])} ({x['qualification']}, {x['experience_years']} yrs exp)"
            for i, x in enumerate(docs, 1))
        return (
            f"Here are the available doctors in {d['dept_name']}:\n{lines}\n\n"
            f"Who would you like to see?",
            [dn(x["name"]) for x in docs],
        )

    if step == "date":
        doctor = dn(d.get("doctor_name", "the doctor"))
        return (
            f"What date would you like to see {doctor}? "
            "You can say 'today', 'tomorrow', or a date like 26 Oct 2026.",
            ["Today", "Tomorrow"],
        )

    if step == "slot":
        slots = free_slots(d["doctor_id"], d["apt_date"])
        if not slots:
            return (
                f"Sorry, {dn(d['doctor_name'])} has no free slots on "
                f"{d['apt_date'].strftime('%d %b %Y')}. Please choose another date.",
                [],
            )
        lines = "\n".join(f"  {i}. {fmt_slot(s)}" for i, s in enumerate(slots, 1))
        return (
            f"Here are the available time slots for "
            f"{d['apt_date'].strftime('%d %b %Y')}:\n{lines}\n\n"
            f"Which time works best for you?",
            [fmt_slot(s) for s in slots],
        )

    return "Could you tell me more?", []


# BOT REPLY (acknowledgement): short friendly line added before the next question,
# e.g. "Nice to meet you, Ved!" or "Got it, noted your number."
def _ack(step: str, d: dict) -> str:
    """Short friendly acknowledgement after each field is saved."""
    if step == "name":   return f"Nice to meet you, {d.get('name', '')}! "
    if step == "phone":  return "Got it, noted your number. "
    if step == "email":  return "Perfect, I have your email. "
    if step == "dept":   return f"Sure, {d.get('dept_name', '')}. "
    if step == "doctor": return f"Great choice — {dn(d.get('doctor_name', ''))}! "
    if step == "date":
        dt = d.get("apt_date")
        return f"Got it, {dt.strftime('%d %b %Y')}. " if dt else ""
    return ""


# ── field validation ─────────────────────────────────────────────────────────

# VALIDATION: checks what the user typed for the current step and saves it into the booking data.
# Returns an error message (shown to the user as the bot reply) if invalid, or None if OK.
# Includes the rules: valid phone, valid email, date not in the past and within MAX_DAYS,
# and the doctor must have free slots.
def validate_step(step: str, text: str, d: dict):
    """
    Try to fill 'step' from 'text'.
    Returns error string on failure, None on success.
    """
    if step == "name":
        candidate = NAME_NOISE.sub("", text.strip()).strip(" .!,")
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", candidate):
            candidate = text.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", candidate.strip()):
            d["name"] = " ".join(candidate.strip().title().split())
            return None
        return "I didn't catch that. Could you tell me your full name (letters only)?"

    if step == "phone":
        phone = extract_phone(text)
        if not phone:
            return ("That doesn't look like a valid 10-digit Indian mobile number. "
                    "Please re-enter it.")
        d["phone"] = phone
        return None

    if step == "email":
        email = extract_email(text)
        if not email:
            return "That email doesn't look right. Please enter it like name@example.com."
        d["email"] = email
        return None

    if step == "dept":
        depts = list_departments()
        dept  = match_department(text, depts)
        if not dept:
            tl = text.lower()
            for dk, aliases in DEPT_ALIASES.items():
                if any(re.search(r"\b" + re.escape(a) + r"\b", tl) for a in aliases):
                    dept = next((x for x in depts if x["name"].lower() == dk), None)
                    if dept:
                        break
        if not dept:
            return "I couldn't match that to a department. Please choose from the list above."
        d["dept_id"], d["dept_name"] = dept["id"], dept["name"]
        return None

    if step == "doctor":
        docs = list_doctors(d["dept_id"])
        doc  = match_doctor(text, docs)
        if not doc:
            return "I couldn't find that doctor. Please choose from the options above."
        d["doctor_id"], d["doctor_name"] = doc["id"], doc["name"]
        return None

    if step == "date":
        dt = extract_date(text)
        if not dt:
            return ("I couldn't understand that date. "
                    "Try '26 Oct 2026', '2026-10-26', or 'tomorrow'.")
        today = date.today()
        if dt < today:
            return "That date has already passed. Please choose today or a future date."
        if dt > today + timedelta(days=MAX_DAYS):
            return (f"Bookings are only open for the next {MAX_DAYS} days. "
                    "Please choose a closer date.")
        if not free_slots(d["doctor_id"], dt):
            return (f"Sorry, {dn(d['doctor_name'])} has no free slots on "
                    f"{dt.strftime('%d %b %Y')}. Please try another date.")
        d["apt_date"] = dt
        return None

    if step == "slot":
        slots = free_slots(d["doctor_id"], d["apt_date"])
        slot  = extract_slot(text, slots)
        if not slot:
            # Try parsing any time and find nearest slot
            try:
                parsed_t = dateparser.parse(text.strip(), fuzzy=True)
                if parsed_t:
                    hhmm = parsed_t.strftime("%H:%M")
                    def slot_dist(s):
                        sh, sm = map(int, s.split(":"))
                        ph, pm = map(int, hhmm.split(":"))
                        return abs(sh * 60 + sm - ph * 60 - pm)
                    best = min(reversed(slots), key=slot_dist, default=None)
                    if best and slot_dist(best) <= 90:
                        slot = best
            except Exception:
                pass
        if not slot:
            return "That time slot isn't available. Please choose from the options above."
        d["slot"] = slot
        return None

    return None


# ── appointment confirmation text ────────────────────────────────────────────

# BOT REPLY (summary): the booking summary shown before the final Yes/No confirmation.
def confirmation_text(d: dict) -> str:
    return (
        f"Here is a summary of your appointment before I confirm it:\n\n"
        f"  Patient    : {d['name']}\n"
        f"  Phone      : {d['phone']}\n"
        f"  Email      : {d['email']}\n"
        f"  Department : {d['dept_name']}\n"
        f"  Doctor     : {dn(d['doctor_name'])}\n"
        f"  Date       : {d['apt_date'].strftime('%d %b %Y')}\n"
        f"  Time       : {fmt_slot(d['slot'])}\n\n"
        f"Shall I go ahead and book this? Reply Yes to confirm or No to cancel."
    )


# ── guaranteed DB insert ─────────────────────────────────────────────────────

# DATABASE INSERT: saves the appointment in PostgreSQL and returns its booking ID.
# Safe against duplicates and double-booking. Errors are raised (never hidden) so the caller can react.
def do_insert(d: dict) -> int:
    """
    Insert appointment into PostgreSQL. Returns integer booking ID.
    Raises psycopg2 exceptions on failure — NEVER swallows errors.
    """
    # Takes a connection from the pool and uses a manual transaction (committed only at the end).
    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Duplicate protection: if this booking_key was already saved (e.g. user clicked confirm twice),
            # return the existing booking ID instead of creating another one.
            cur.execute("SELECT id FROM appointments WHERE booking_key=%s", (d["booking_key"],))
            existing = cur.fetchone()
            if existing:
                conn.commit()
                return existing[0]
            # Locks the doctor's row (FOR UPDATE) and checks the doctor is still available in that department.
            cur.execute(
                """SELECT department_id, available FROM doctors
                   WHERE id=%s FOR UPDATE""",
                (d["doctor_id"],),
            )
            doctor = cur.fetchone()
            if not doctor or not doctor[1] or doctor[0] != d["dept_id"]:
                raise ValueError("The selected doctor is no longer available in that department.")
            # Final validation: the slot must be a valid slot and the date/time must not be in the past.
            if (d["slot"] not in SLOTS or d["apt_date"] < date.today()
                    or (d["apt_date"] == date.today()
                        and d["slot"] <= datetime.now().strftime("%H:%M"))):
                raise ValueError("The selected appointment date or time is invalid.")
            # THE ACTUAL INSERT into the appointments table. ON CONFLICT DO NOTHING + the unique indexes
            # make sure the same slot can't be booked twice.
            cur.execute(
                """INSERT INTO appointments
                   (patient_name, phone, email, doctor_id, appointment_date, time_slot, booking_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (booking_key) DO NOTHING RETURNING id""",
                (
                    d["name"], d["phone"], d["email"],
                    d["doctor_id"], d["apt_date"], d["slot"], d["booking_key"],
                ),
            )
            inserted = cur.fetchone()
            if inserted:
                appt_id = inserted[0]
            else:
                cur.execute("SELECT id FROM appointments WHERE booking_key=%s", (d["booking_key"],))
                appt_id = cur.fetchone()[0]
        # Commit: the booking is now permanently saved in the database.
        conn.commit()
        print(f"[db] Appointment insert committed (id={appt_id}).")
        return appt_id
    # Any error -> undo the whole transaction, then raise the error again.
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass
        pool.putconn(conn)


# BOT REPLY (final confirmation): handles the user's Yes / No after the booking summary.
# Yes -> calls do_insert() to save to the DB, then replies "Your appointment is confirmed" with the
# booking ID. If the slot was taken meanwhile, offers the remaining slots or asks for another date.
# No -> cancels the booking. Anything else -> asks again.
def handle_confirm(text: str, s: dict) -> tuple:
    """Process YES/NO at the confirm step. Returns (reply, options)."""
    d = s["data"]

    if is_yes(text):
        d.setdefault("booking_key", secrets.token_urlsafe(32))
        try:
            appt_id = do_insert(d)
        # Slot conflict: someone else booked this slot a moment ago (blocked by the unique index).
        except (psycopg2.errors.UniqueViolation, psycopg2.IntegrityError) as e:
            print("[db] Slot conflict:", repr(e))
            d.pop("slot", None)
            slots = free_slots(d["doctor_id"], d["apt_date"])
            if slots:
                s["state"] = "booking"
                s["step"]  = "slot"
                lines = "\n".join(
                    f"  {i}. {fmt_slot(sl)}" for i, sl in enumerate(slots, 1))
                return (
                    f"That slot was just taken by someone else! "
                    f"Here are the remaining free slots on "
                    f"{d['apt_date'].strftime('%d %b %Y')}:\n{lines}",
                    [fmt_slot(sl) for sl in slots],
                )
            d.pop("apt_date", None)
            s["state"] = "booking"
            s["step"]  = "date"
            return (
                f"That slot was taken and no others are free on that day. "
                f"Please choose another date for {dn(d['doctor_name'])}.",
                [],
            )
        except Exception as e:
            print("[db] Insert failed:", repr(e))
            return (
                "Sorry, something went wrong saving your booking. "
                "Please try again in a moment.",
                ["Yes, confirm", "No, cancel"],
            )

        # Booking saved successfully: reply with the confirmation message and the booking ID.
        s["state"] = "done"
        return (
            f"Your appointment is confirmed!\n\n"
            f"  Booking ID : #{appt_id}\n"
            f"  Patient    : {d['name']}\n"
            f"  Phone      : {d['phone']}\n"
            f"  Doctor     : {dn(d['doctor_name'])} ({d['dept_name']})\n"
            f"  Date       : {d['apt_date'].strftime('%d %b %Y')}\n"
            f"  Time       : {fmt_slot(d['slot'])}\n\n"
            f"See you then!\n"
            f"Is there anything else I can help you with, "
            f"or would you like to make another booking?",
            [],
        )

    if is_no(text):
        s["state"] = "done"
        return (
            "Booking cancelled. No worries — type anything whenever "
            "you'd like to make a new appointment.",
            [],
        )

    # Unclear answer — show summary again
    return (
        "Please just say Yes to confirm or No to cancel.\n\n" + confirmation_text(d),
        ["Yes, confirm", "No, cancel"],
    )


# ── opportunistic extraction from any message ─────────────────────────────────

# SMART SKIP: if the user gives extra details in one message (e.g. "book cardiologist tomorrow,
# my number is 98xxxxxxxx"), fill those later fields now so the bot doesn't ask for them again.
# Returns the first step that is still missing.
def opportunistic_extract(text: str, d: dict, current_step: str) -> str:
    """
    Try to fill FUTURE fields from this message so we can skip steps.
    Never tries to fill the current step — validate_step() handles that.
    Returns the new current_step (may skip ahead if a field was serendipitously filled).
    """
    step = current_step

    # Only extract phone if it's NOT the current step (validate_step handles that)
    if step != "phone" and not d.get("phone"):
        ph = extract_phone(text)
        if ph:
            d["phone"] = ph

    # Only extract email if it's NOT the current step
    if step != "email" and not d.get("email"):
        em = extract_email(text)
        if em:
            d["email"] = em

    # Only extract dept/doctor if NOT the current step
    if step not in ("dept", "doctor") and not d.get("dept_id"):
        depts = list_departments()
        dept  = match_department(text, depts)
        if not dept:
            tl = text.lower()
            for dk, aliases in DEPT_ALIASES.items():
                if any(re.search(r"\b" + re.escape(a) + r"\b", tl) for a in aliases):
                    dept = next((x for x in depts if x["name"].lower() == dk), None)
                    if dept:
                        break
        if dept:
            d["dept_id"], d["dept_name"] = dept["id"], dept["name"]

    if step != "doctor" and d.get("dept_id") and not d.get("doctor_id"):
        docs = list_doctors(d["dept_id"])
        doc  = match_doctor(text, docs)
        if doc:
            d["doctor_id"], d["doctor_name"] = doc["id"], doc["name"]

    # Only extract date if NOT the current step
    if step != "date" and not d.get("apt_date") and d.get("doctor_id"):
        dt = extract_date(text)
        if dt:
            today = date.today()
            if today <= dt <= today + timedelta(days=MAX_DAYS):
                if free_slots(d["doctor_id"], dt):
                    d["apt_date"] = dt

    # Only extract slot if NOT the current step
    if step != "slot" and d.get("doctor_id") and d.get("apt_date") and not d.get("slot"):
        slots = free_slots(d["doctor_id"], d["apt_date"])
        slot  = extract_slot(text, slots)
        if slot:
            d["slot"] = slot

    # Recalculate the step based on what's now filled
    for s in STEP_ORDER:
        key = "dept_id" if s == "dept" else ("doctor_id" if s == "doctor" else ("apt_date" if s == "date" else s))
        if not d.get(key):
            return s
    return "__done__"


# ── main dispatch ─────────────────────────────────────────────────────────────

# MAIN DISPATCH helpers. STEP_ORDER = the order in which the booking questions are asked.
STEP_ORDER = ["name", "phone", "email", "dept", "doctor", "date", "slot"]


# CHANGE-ANYTHING SUPPORT ---------------------------------------------------
# Words meaning "I want to change something" + which field they mean.
CHANGE_VERB_RE = re.compile(
    r"\b(change|another|different|edit|update|modify|wrong|incorrect|switch|"
    r"go back|redo|re-?enter|reschedule|select another|choose another)\b", re.I)
# Order matters: the first match wins.
CHANGE_FIELDS = [
    ("dept",   re.compile(r"\b(department|dept|speciality|specialty)\b", re.I)),
    ("doctor", re.compile(r"\b(doctor|doc)\b", re.I)),
    ("date",   re.compile(r"\b(date|day)\b", re.I)),
    ("slot",   re.compile(r"\b(time|slot)\b", re.I)),
    ("email",  re.compile(r"\b(email|e-mail|mail)\b", re.I)),
    ("phone",  re.compile(r"\b(phone|mobile|number|contact)\b", re.I)),
    ("name",   re.compile(r"\bname\b", re.I)),
]
# What must be cleared when a field changes (later fields depend on earlier ones).
CHANGE_CLEARS = {
    "name":   ["name"],
    "phone":  ["phone"],
    "email":  ["email"],
    "dept":   ["dept_id", "dept_name", "doctor_id", "doctor_name", "apt_date", "slot"],
    "doctor": ["doctor_id", "doctor_name", "apt_date", "slot"],
    "date":   ["apt_date", "slot"],
    "slot":   ["slot"],
}

# Returns which field the user wants to change ("dept", "doctor", ...) or None.
def detect_change_field(text: str):
    if len(text.split()) > 12 or not CHANGE_VERB_RE.search(text):
        return None
    for field, rx in CHANGE_FIELDS:
        if rx.search(text):
            return field
    return None

# Clears that field (and the ones that depend on it), asks the question again,
# and after the user answers the normal flow moves forward / back to the summary.
def start_field_change(s: dict, field: str, message: str):
    d = s["data"]
    for key in CHANGE_CLEARS[field]:
        d.pop(key, None)
    d.pop("booking_key", None)
    s["state"] = "booking"
    s["step"]  = field
    # If the message already contains the new value ("change my phone to 98..."), use it directly.
    if field in ("phone", "email", "date") and validate_step(field, message, d) is None:
        s["step"] = next_step_after(field, d)
        if all_fields_done(d) or s["step"] == "__done__":
            d["booking_key"] = secrets.token_urlsafe(32)
            s["state"], s["step"] = "confirm", "__done__"
            return "Updated.\n\n" + confirmation_text(d), ["Yes, confirm", "No, cancel"]
        prompt, options = ask_for(s["step"], d)
        return "Updated. " + prompt, options
    prompt, options = ask_for(field, d)
    return "Sure, let's change that. " + prompt, options


# Returns the first booking step that has no value yet (or "__done__" if everything is filled).
def first_missing_step(d: dict) -> str:
    keys = {
        "name": "name", "phone": "phone", "email": "email",
        "dept": "dept_id", "doctor": "doctor_id", "date": "apt_date", "slot": "slot",
    }
    return next((step for step in STEP_ORDER if not d.get(keys[step])), "__done__")


# Matches text against ALL departments in the DB (not only ones with available doctors).
def match_any_department(text: str):
    departments = q("SELECT id, name FROM departments ORDER BY name")
    return match_department(text, departments)


# BOT REPLY: text list of the available doctors in a department.
def list_department_doctors(dept_id: int) -> str:
    doctors = list_doctors(dept_id)
    if not doctors:
        return "There are no doctors currently available for booking in that department."
    return "Doctors available in this department:\n" + "\n".join(
        f"  - {dn(doc['name'])} | {doc['qualification']} | "
        f"{doc['experience_years']} yrs experience"
        for doc in doctors
    )


# Saves the (user message, bot reply) pair in the session history; keeps only the last 20.
def record_turn(s: dict, message: str, reply: str):
    s["history"].append((message, reply))
    del s["history"][:-20]


# Re-asks the question for the step the user is currently on (used after answering a side question).
def prompt_for_current_step(s: dict):
    if s["state"] == "confirm":
        return confirmation_text(s["data"]), ["Yes, confirm", "No, cancel"]
    if s["state"] == "booking":
        return ask_for(s["step"], s["data"])
    return "", []


# True if the message sounds like the user wants to book ("book", "appointment", "need a doctor", ...).
def is_booking_request(text: str) -> bool:
    return bool(re.search(
        r"\b(book|booking|appointment|schedule|make an appointment|see a doctor|"
        r"need a doctor|want a doctor|need a specialist|want a specialist|"
        r"need a dermatologist|want a dermatologist|need a physician|want a physician|"
        r"need to see (?:a )?(?:doctor|specialist|dermatologist|physician)|"
        r"want to see (?:a )?(?:doctor|specialist|dermatologist|physician))\b", text, re.I
    ))


# True if the user seems to want a different department.
def is_department_change(text: str, step: str) -> bool:
    return step in ("dept", "doctor") or bool(re.search(
        r"\b(change|switch|instead|actually|rather|no[, ]|want|need)\b", text, re.I
    ))


# Starts (or restarts) a booking. keep_contact=True keeps name/phone/email for a repeat booking.
def start_booking(s: dict, keep_contact: bool = False):
    contact = {key: s["data"].get(key) for key in ("name", "phone", "email")} if keep_contact else {}
    s["data"] = {key: value for key, value in contact.items() if value}
    s["state"] = "booking"
    s["step"] = first_missing_step(s["data"])
    if s["step"] == "__done__":
        s["step"] = "dept"


# BOT REPLY: user switched department mid-booking - clears doctor/date/slot and shows the new
# department's doctors.
def change_department(s: dict, dept: dict, message: str):
    data = s["data"]
    data["dept_id"], data["dept_name"] = dept["id"], dept["name"]
    for key in ("doctor_id", "doctor_name", "apt_date", "slot", "booking_key"):
        data.pop(key, None)
    s["state"] = "booking"
    if s["step"] in ("dept", "doctor", "date", "slot") or s["step"] == "__done__":
        s["step"] = "doctor"
    doctors_text = list_department_doctors(dept["id"])
    if s["step"] != "doctor":
        prompt, _ = ask_for(s["step"], data)
        reply = f"Of course, let's switch to {dept['name']}. {doctors_text}\n\n{prompt}"
    else:
        reply = f"Of course, let's switch to {dept['name']}. {doctors_text}"
        if list_doctors(dept["id"]):
            reply += "\n\nWhich doctor would you like to see?"
    return reply, [dn(doc["name"]) for doc in list_doctors(dept["id"])]


# BOT REPLY (small talk): scripted answers for greetings, goodbye, thanks, "help", and a default fallback.
def handle_general_message(text: str, s: dict):
    tl = text.strip().lower()
    if re.fullmatch(r"(hi|hello|hey|good morning|good afternoon|good evening)[!. ]*", tl):
        return "Hello! How can I help you today?", []
    if re.fullmatch(r"(bye|goodbye|see you|good night)[!. ]*", tl):
        return "Goodbye! Take care, and thank you for contacting City Care Hospital.", []
    if re.fullmatch(r"(thanks|thank you|thx)[!. ]*", tl):
        return "You're welcome! Is there anything else I can help you with?", []
    if re.fullmatch(r"(bye|goodbye|see you|good night)[!. ]*", tl):
        return "Goodbye! Take care, and thank you for contacting City Care Hospital.", []
    if re.search(r"\b(help|what can you do)\b", tl):
        return ("I can share information about our configured doctors and departments, "
                "or help you book an appointment. What would you like to do?", [])
    if re.search(r"\b(don't know|do not know|not sure)\b", tl) and "department" in tl:
        return ("No problem. If you briefly describe your concern, I can suggest a department "
                "to consider, but I can't diagnose you.", [])
    return ("I can help with our configured doctor directory or booking appointments. "
            "What would you like to know?", [])


# Returns the next booking step after the current one, skipping fields that are already filled.
def next_step_after(step: str, d: dict) -> str:
    """Return next STEP_ORDER entry, skipping already-filled fields."""
    try:
        idx = STEP_ORDER.index(step) + 1
    except ValueError:
        idx = len(STEP_ORDER)

    while idx < len(STEP_ORDER):
        candidate = STEP_ORDER[idx]
        if candidate == "dept"   and d.get("dept_id"):
            idx += 1; continue
        if candidate == "doctor" and d.get("doctor_id"):
            idx += 1; continue
        if candidate == "date"   and d.get("apt_date"):
            idx += 1; continue
        if candidate == "slot"   and d.get("slot"):
            idx += 1; continue
        if candidate not in ("dept","doctor","date","slot") and d.get(candidate):
            idx += 1; continue
        return candidate
    return "__done__"


# True when name, phone, email, department, doctor, date and slot are all filled.
def all_fields_done(d: dict) -> bool:
    return all(d.get(k) for k in (
        "name", "phone", "email", "dept_id", "doctor_id", "apt_date", "slot"))


# =====================================================================
# MAIN CHATBOT FUNCTION - one call per user message. Returns {"reply": text, "options": [chips]}
# which /api/chat sends back to the website. Everything below runs top to bottom, and the FIRST
# matching rule produces the bot's reply:
#   1. load the session  2. "__start__" greeting  3. empty message  4. emergency words
#   5. department change  6. change doctor / date / time  7. info questions  8. hi / thanks / cancel
#   9. idle -> start booking  10. confirm Yes/No  11. fill the current booking step  12. summary
# =====================================================================
def chat_turn(sid: str, user_msg: str) -> dict:
    # Get this user's session: memory first, then the database, otherwise create a new one.
    if sid not in sessions:
        sessions[sid] = load_session(sid) or new_session()
    s = sessions[sid]
    message = user_msg.strip()

    # Page just opened (the website sends "__start__" automatically): show the welcome message,
    # or "Welcome back" + the current question if the user already has a conversation.
    if message == "__start__":
        if s["history"]:
            if s["state"] == "booking":
                reply, options = ask_for(s["step"], s["data"])
                reply = "Welcome back. " + reply
            elif s["state"] == "confirm":
                reply, options = confirmation_text(s["data"]), ["Yes, confirm", "No, cancel"]
            else:
                reply, options = f"Welcome back to {HOSPITAL_NAME}. How can I help you?", []
        else:
            reply = (
                f"Hello! Welcome to {HOSPITAL_NAME}. I'm Aria. "
                "How can I help you today? I can answer questions about our doctors "
                "or help you book an appointment."
            )
            options = []
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # Empty message -> ask the user to type something.
    if not message:
        return {"reply": "Please send a message so I can help.", "options": []}

    # BOT REPLY (emergency): possible emergency detected -> tell the user to contact emergency services.
    if EMERGENCY_RE.search(message):
        reply = (
            "If this may be an emergency, contact your local emergency services or go "
            "to the nearest emergency department now. Do not wait to book an appointment."
        )
        record_turn(s, message, reply)
        return {"reply": reply, "options": []}

    # d = booking details collected so far, step = current question, lower = lower-cased message.
    d = s["data"]
    step = s["step"]
    lower = message.lower()

    # User wants to change any already-given field (works in booking and confirm states).
    if s["state"] in ("booking", "confirm"):
        change_field = detect_change_field(message)
        if change_field and not match_any_department(message):
            reply, options = start_field_change(s, change_field, message)
            record_turn(s, message, reply)
            return {"reply": reply, "options": options}



    # Resolve department changes before interpreting text as a doctor name or a field value.
    # DEPARTMENT CHANGE: if the user names a (different) department during booking, switch to it.
    requested_dept = None
    if message.isdigit() and step == "dept":
        requested_dept = match_department(message, list_departments())
    elif not message.isdigit():
        requested_dept = match_any_department(message)
    if (requested_dept and s["state"] in ("booking", "confirm")
            and not is_info_question(message)
            and (is_department_change(message, step)
                 or requested_dept["id"] != d.get("dept_id"))):
        reply, options = change_department(s, requested_dept, message)
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # Changes are handled before the confirmation gate so a pending booking can be corrected.
    # CHANGE REQUESTS: lets the user change doctor, date or time even after the summary was shown.
    if s["state"] in ("booking", "confirm"):
        # "change / another doctor" -> clear doctor, date and slot, and show the doctors again.
        if re.search(r"\b(another|different|change|switch|not this one|not this doctor|don't want|do not want|dont want)\b", lower) and re.search(r"\b(doctor|one)\b", lower):
            d.pop("doctor_id", None)
            d.pop("doctor_name", None)
            d.pop("apt_date", None)
            d.pop("slot", None)
            d.pop("booking_key", None)
            s["state"], s["step"] = "booking", "doctor"
            reply = list_department_doctors(d["dept_id"])
            if list_doctors(d["dept_id"]):
                reply += "\n\nWhich doctor would you like to see?"
            record_turn(s, message, reply)
            return {"reply": reply, "options": [dn(doc["name"]) for doc in list_doctors(d["dept_id"])]}

        # "change the date" -> clear date and slot, and ask for a new date (or go straight to slots if a valid date was typed).
        if re.search(r"\b(change|move|reschedule|different|another)\b", lower) and re.search(r"\b(date|day)\b", lower):
            d.pop("apt_date", None)
            d.pop("slot", None)
            d.pop("booking_key", None)
            s["state"], s["step"] = "booking", "date"
            requested_date = extract_date(message)
            if requested_date:
                if requested_date < date.today() or requested_date > date.today() + timedelta(days=MAX_DAYS):
                    reply = f"Please choose a date from today through the next {MAX_DAYS} days."
                elif not free_slots(d["doctor_id"], requested_date):
                    reply = f"There are no open slots for {dn(d['doctor_name'])} on {requested_date:%d %b %Y}. Please choose another date."
                else:
                    d["apt_date"] = requested_date
                    s["step"] = "slot"
                    reply, options = ask_for("slot", d)
                    record_turn(s, message, reply)
                    return {"reply": reply, "options": options}
            else:
                reply, _ = ask_for("date", d)
            record_turn(s, message, reply)
            return {"reply": reply, "options": ["Today", "Tomorrow"]}

        # "change the time" -> clear the slot and show the free slots again.
        if re.search(r"\b(change|move|reschedule|different|another)\b", lower) and re.search(r"\b(time|slot)\b", lower):
            d.pop("slot", None)
            d.pop("booking_key", None)
            s["state"], s["step"] = "booking", "slot"
            reply, options = ask_for("slot", d)
            record_turn(s, message, reply)
            return {"reply": reply, "options": options}

        # User said "no" to the doctor choice -> offer another doctor or a different department.
        if s["step"] == "doctor" and is_no(message):
            reply = "No problem. Would you like another doctor in this department, or a different department?"
            record_turn(s, message, reply)
            return {"reply": reply, "options": [doc["name"] for doc in list_doctors(d["dept_id"])]}

    # Answer questions before validating the current booking field. This prevents questions
    # such as "show me doctors" from being mistaken for a patient name.
    # INFO QUESTIONS: answered from the database (quick_doctor_answer); if a booking is in progress,
    # the current booking question is repeated after the answer.
    if is_info_question(message):
        reply = quick_doctor_answer(message)
        if not reply and re.search(r"\b(available|availability|open slots?|times?)\b", lower):
            if d.get("doctor_id") and d.get("apt_date"):
                reply, options = ask_for("slot", d)
            elif d.get("doctor_id"):
                reply, options = ask_for("date", d)
            else:
                reply = "Appointment slots depend on the doctor and date. I can check them once you choose both."
                options = []
        else:
            options = []
            if not reply:
                reply = (
                    "I can only answer using the hospital information configured here. "
                    "I can help with doctor details, departments, or appointment booking."
                )
        if s["state"] in ("booking", "confirm"):
            prompt, prompt_options = prompt_for_current_step(s)
            if prompt:
                reply += "\n\n" + prompt
                options = prompt_options
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # Greeting -> BOT REPLY "Hello!" (plus the current booking question if a booking is in progress).
    if re.fullmatch(r"(hi|hello|hey|good morning|good afternoon|good evening)[!. ]*", lower):
        reply, options = handle_general_message(message, s)
        if s["state"] in ("booking", "confirm"):
            prompt, options = prompt_for_current_step(s)
            reply += "\n\n" + prompt
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # "Thanks" -> BOT REPLY "You're welcome!".
    if re.fullmatch(r"(thanks|thank you|thx)[!. ]*", lower):
        reply, options = handle_general_message(message, s)
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # User typed "cancel" during booking -> drop the booking details, nothing is saved.
    if s["state"] == "booking" and re.fullmatch(r"(cancel|cancel booking|stop booking|abort)[!. ]*", lower):
        s["state"], s["data"] = "idle", {}
        reply = "No problem. I haven't submitted a booking. Let me know if you'd like to start again."
        record_turn(s, message, reply)
        return {"reply": reply, "options": []}

    # Previous booking finished and the user wants another one -> restart but keep name/phone/email.
    if s["state"] == "done" and is_booking_request(message):
        start_booking(s, keep_contact=True)
        reply, options = ask_for(s["step"], d)
        record_turn(s, message, "Of course, let's arrange another appointment. " + reply)
        return {"reply": "Of course, let's arrange another appointment. " + reply, "options": options}

    # Booking finished and the user is just chatting -> small-talk replies.
    if s["state"] == "done":
        reply, options = handle_general_message(message, s)
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # No booking in progress: start one if they asked to book, suggest a department if they described
    # a problem, otherwise give a general reply.
    if s["state"] == "idle":
        if is_booking_request(message):
            start_booking(s)
            reply, options = ask_for(s["step"], d)
            reply = "I'd be happy to help you book an appointment. " + reply
        else:
            suggested_dept = match_any_department(message)
            if suggested_dept and not re.search(
                    r"\b(doctor|doctors|specialist|department|book|appointment)\b", lower):
                reply = (
                    f"{suggested_dept['name']} may be a department to consider based on what "
                    "you described. This isn't a diagnosis. Would you like to see its doctors "
                    "or book an appointment?"
                )
                options = ["Show doctors", "Book an appointment"]
            else:
                reply, options = handle_general_message(message, s)
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # "restart" / "start over" / "reset" -> start a fresh booking.
    if message.lower() in RESET_WORDS:
        start_booking(s)
        reply, options = ask_for(s["step"], d)
        reply = "Sure, let's start a new booking. " + reply
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # CONFIRM STEP: the summary was shown - handle Yes (saves to the database) or No (cancels).
    if s["state"] == "confirm":
        if is_no(message) or re.search(r"\b(cancel|stop|abort)\b", lower):
            s["state"] = "idle"
            s["data"] = {}
            reply = "No problem. I haven't submitted that booking. Let me know if you'd like to start again."
            record_turn(s, message, reply)
            return {"reply": reply, "options": []}
        reply, options = handle_confirm(message, s)
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # Treat bare "no" as a request for clarification, never as a name or doctor.
    # A bare "no" on the name / phone / email / department step is not an answer -> just ask the question again.
    if is_no(message) and s["step"] in ("name", "phone", "email", "dept"):
        reply, options = ask_for(s["step"], d)
        reply = "No problem. " + reply
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    step = s["step"]
    # BOOKING FLOW: first try to fill later fields from the same message (skip ahead if possible).
    opportunistic_step = opportunistic_extract(message, d, step)
    if opportunistic_step != step:
        s["step"] = opportunistic_step
        step = opportunistic_step

    # Everything is already collected -> go straight to the confirmation summary.
    if all_fields_done(d) or step == "__done__":
        d.setdefault("booking_key", secrets.token_urlsafe(32))
        s["state"], s["step"] = "confirm", "__done__"
        reply = confirmation_text(d)
        record_turn(s, message, reply)
        return {"reply": reply, "options": ["Yes, confirm", "No, cancel"]}

    # Validate what the user typed for the current step. If invalid -> BOT REPLY = error message + same question again.
    err = validate_step(step, message, d)
    if err:
        prompt, options = ask_for(step, d)
        reply = err + "\n\n" + prompt
        record_turn(s, message, reply)
        return {"reply": reply, "options": options}

    # Valid answer: build the reply = short acknowledgement + the next question
    # (or the final summary if that was the last step), then return it.
    ack = _ack(step, d)
    s["step"] = next_step_after(step, d)
    if all_fields_done(d) or s["step"] == "__done__":
        d.setdefault("booking_key", secrets.token_urlsafe(32))
        s["state"], s["step"] = "confirm", "__done__"
        reply = ack + "\n\n" + confirmation_text(d)
        options = ["Yes, confirm", "No, cancel"]
    else:
        prompt, options = ask_for(s["step"], d)
        reply = ack + prompt
    record_turn(s, message, reply)
    return {"reply": reply, "options": options}


# FASTAPI APP + STARTUP: on startup connect/create the database (stop if PostgreSQL is unreachable)
# and load the AI model in a background thread so the server starts immediately.
@asynccontextmanager
async def lifespan(app):
    try:
        init_db()
    except Exception as e:
        print("\n[db] Cannot connect to PostgreSQL:", e); raise
    threading.Thread(target=load_llm, daemon=True).start()
    yield

# The web application object. Routes below: "/" = user chat page, "/admin" = admin page.
app = FastAPI(title="Hospital Appointment Booking", lifespan=lifespan)

@app.get("/")
def user_page():  return FileResponse(BASE_DIR / "static" / "index.html")

@app.get("/admin")
def admin_page(): return FileResponse(BASE_DIR / "static" / "admin.html")

# Called by the website every few seconds to show whether the AI model is loading / ready / failed.
@app.get("/api/llm-status")
def llm_status():
    return {"ready": llm["ready"], "status": llm["status"], "error": llm["error"]}

# Shape of the data the website sends to /api/chat: session_id (identifies the user's chat) and message.
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message:    str = Field(default="", max_length=500)

# CHAT ENDPOINT: the website sends each user message here. It runs chat_turn() to get the bot reply,
# saves the conversation state to the database, and returns {"reply": ..., "options": [...]}.
@app.post("/api/chat")
def chat(body: ChatIn):
    # Remember when this request started (used for the minimum reply time below).
    start = time.monotonic()                                   # ADDED
    # Only one message per session is processed at a time (prevents double processing / double booking).
    with _session_lock(body.session_id):
        result = chat_turn(body.session_id, body.message.strip())
        try:
            # DATABASE: persist the updated conversation state after every message.
            save_session(body.session_id, sessions[body.session_id])
        except Exception as e:
            print("[session] Could not persist conversation state:", repr(e))
            if sessions[body.session_id]["state"] == "done":
                return result
            raise HTTPException(
                503,
                "Your conversation could not be saved. Please retry; if you just confirmed a booking, contact the hospital before submitting it again.",
            )
        # Delay: pads the response so every reply takes at least 9 seconds in total
        # (the typing dots stay visible on the website meanwhile).
        time.sleep(max(0, 9 - (time.monotonic() - start)))     # ADDED
        return result

# Admin API
# ADMIN API: login tokens that are currently valid (kept in memory - cleared when the server restarts).
admin_tokens: set = set()

# Security check added to every admin route: the request header X-Admin-Token must be a valid login token.
def require_admin(x_admin_token: str = Header(default="")):
    if x_admin_token not in admin_tokens:
        raise HTTPException(401, "Please log in as admin.")

# Request-body models: the JSON each admin endpoint accepts, with validation (length limits, numeric ranges).
class LoginIn(BaseModel):        password: str
class DoctorIn(BaseModel):
    name:             str  = Field(min_length=2, max_length=100)
    department_id:    int
    qualification:    str  = Field(default="", max_length=150)
    experience_years: int  = Field(default=0, ge=0, le=70)
    details:          str  = ""
    available:        bool = True
class AvailabilityIn(BaseModel): available: bool
class DepartmentIn(BaseModel):   name: str = Field(min_length=2, max_length=100)
class StatusIn(BaseModel):       status: str

# Admin login: compares the password with ADMIN_PASSWORD and returns a random token.
@app.post("/api/admin/login")
def admin_login(body: LoginIn):
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong password.")
    token = secrets.token_hex(24)
    admin_tokens.add(token)
    return {"token": token}

# Admin: list departments with the number of doctors in each.
@app.get("/api/admin/departments", dependencies=[Depends(require_admin)])
def get_departments():
    return q("""SELECT d.id, d.name, count(x.id)::int AS doctors
                FROM departments d LEFT JOIN doctors x ON x.department_id = d.id
                GROUP BY d.id ORDER BY d.name""")

# Admin: add a new department (rejects duplicate names).
@app.post("/api/admin/departments", dependencies=[Depends(require_admin)])
def add_department(body: DepartmentIn):
    try:
        return q("INSERT INTO departments (name) VALUES (%s) RETURNING id, name", (body.name.strip(),), one=True)
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(400, "Department already exists.")

# Admin: delete a department (refused if doctors still belong to it).
@app.delete("/api/admin/departments/{dept_id}", dependencies=[Depends(require_admin)])
def delete_department(dept_id: int):
    try:
        q("DELETE FROM departments WHERE id=%s", (dept_id,))
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(400, "This department still has doctors. Remove them first.")
    return {"ok": True}

# Admin: list all doctors with their department name.
@app.get("/api/admin/doctors", dependencies=[Depends(require_admin)])
def get_doctors():
    return q("""SELECT d.*, dp.name AS department FROM doctors d
                JOIN departments dp ON dp.id = d.department_id ORDER BY d.name""")

# Admin: add a new doctor.
@app.post("/api/admin/doctors", dependencies=[Depends(require_admin)])
def add_doctor(body: DoctorIn):
    try:
        return q(
            """INSERT INTO doctors (name, department_id, qualification, experience_years, details, available)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (body.name.strip(), body.department_id, body.qualification.strip(),
             body.experience_years, body.details.strip(), body.available), one=True)
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(400, "Invalid department.")

# Admin: edit all details of an existing doctor.
@app.put("/api/admin/doctors/{doctor_id}", dependencies=[Depends(require_admin)])
def update_doctor(doctor_id: int, body: DoctorIn):
    try:
        row = q(
            """UPDATE doctors SET name=%s, department_id=%s, qualification=%s,
               experience_years=%s, details=%s, available=%s WHERE id=%s RETURNING id""",
            (body.name.strip(), body.department_id, body.qualification.strip(),
             body.experience_years, body.details.strip(), body.available, doctor_id), one=True)
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(400, "Invalid department.")
    if not row: raise HTTPException(404, "Doctor not found.")
    return row

# Admin: quickly mark a doctor available / unavailable (unavailable doctors are hidden from booking).
@app.patch("/api/admin/doctors/{doctor_id}/availability", dependencies=[Depends(require_admin)])
def set_availability(doctor_id: int, body: AvailabilityIn):
    row = q("UPDATE doctors SET available=%s WHERE id=%s RETURNING id", (body.available, doctor_id), one=True)
    if not row: raise HTTPException(404, "Doctor not found.")
    return row

# Admin: delete a doctor (refused if the doctor already has appointments - mark unavailable instead).
@app.delete("/api/admin/doctors/{doctor_id}", dependencies=[Depends(require_admin)])
def delete_doctor(doctor_id: int):
    try:
        q("DELETE FROM doctors WHERE id=%s", (doctor_id,))
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(400, "This doctor has appointments. Mark as unavailable instead.")
    return {"ok": True}

# Admin: list all appointments, optionally filtered by date and/or status.
@app.get("/api/admin/appointments", dependencies=[Depends(require_admin)])
def get_appointments(date: str = "", status: str = ""):
    return q(
        """SELECT a.id, a.patient_name, a.phone, a.email, a.appointment_date, a.time_slot,
                  a.status, a.created_at, d.name AS doctor, dp.name AS department
           FROM appointments a
           JOIN doctors d ON d.id = a.doctor_id
           JOIN departments dp ON dp.id = d.department_id
           WHERE (%s = '' OR a.appointment_date = NULLIF(%s,'')::date)
             AND (%s = '' OR a.status = %s)
           ORDER BY a.appointment_date ASC, a.time_slot ASC, a.id ASC""",
        (date, date, status, status))

# Admin: change an appointment's status (booked / completed / cancelled).
@app.patch("/api/admin/appointments/{appt_id}", dependencies=[Depends(require_admin)])
def set_appointment_status(appt_id: int, body: StatusIn):
    if body.status not in ("booked","completed","cancelled"):
        raise HTTPException(400, "Invalid status.")
    try:
        row = q("UPDATE appointments SET status=%s WHERE id=%s RETURNING id", (body.status, appt_id), one=True)
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(400, "Another booking already holds this slot.")
    if not row: raise HTTPException(404, "Appointment not found.")
    return row

# Starts the web server on http://127.0.0.1:8000 when you run:  python main.py
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
