# 🏥 Hospital Appointment Booking Chatbot

A web-based chatbot ("**Aria**") that lets patients book doctor appointments by chatting, plus an **admin panel** for hospital staff to manage doctors, departments and bookings.

- **Backend:** Python · FastAPI
- **Database:** PostgreSQL
- **Frontend:** plain HTML / CSS / JavaScript (no build step)
- **Optional AI model:** Qwen2.5-0.5B-Instruct (local, offline) — loaded at startup, but **not currently used** by the chat flow (see [AI model note](#-important-note-about-the-ai-model))

---

## 📑 Table of Contents

1. [What this project does](#-what-this-project-does)
2. [Project structure](#-project-structure)
3. [Prerequisites](#-prerequisites)
4. [Setup & run (step by step)](#-setup--run-step-by-step)
5. [Configuration (`.env`)](#-configuration-env)
6. [How it works (the logic)](#-how-it-works-the-logic)
7. [Database design](#-database-design)
8. [API reference](#-api-reference)
9. [Admin panel guide](#-admin-panel-guide)
10. [Important note about the AI model](#-important-note-about-the-ai-model)
11. [Known issues & security notes](#-known-issues--security-notes)
12. [Troubleshooting](#-troubleshooting)
13. [How to customise](#-how-to-customise)

---

## ✅ What this project does

**For patients (`/`)** — a chat window where the bot:

- Asks for name → phone → email → department → doctor → date → time slot
- Shows a **summary** and asks for **Yes / No** before saving
- Answers questions like *"list all doctors"*, *"which departments do you have?"*, *"tell me about Dr. Meera"*
- Suggests a department from keywords (e.g. "I have a skin rash" → Dermatology) — *not a diagnosis*
- Shows an **emergency warning** if the message contains words like *chest pain, stroke, seizure…*
- Lets the user **change** doctor / date / time / department at any time before confirming
- Remembers the conversation if the page is refreshed

**For staff (`/admin`)** — a password-protected page to:

- Add / edit / delete **doctors**, and mark them available / unavailable
- Add / delete **departments**
- View all **bookings**, filter by date/status, and change status (booked / completed / cancelled)

---

## 📁 Project structure

```
hospital-booking/
├── main.py             ← the ENTIRE backend (config, DB, chatbot logic, API routes)
├── requirements.txt    ← Python dependencies
├── .env                ← secrets & settings (DB login, admin password, model folder)
└── static/
    ├── index.html      ← patient chat page   (served at  /)
    └── admin.html      ← admin dashboard     (served at  /admin)
```

> `venv/` and `__pycache__/` are local/generated folders — they should **not** be shared or committed.

### Where to find things inside `main.py`

`main.py` is one long file, organised in labelled sections. Search for these names:

| Section | What it contains |
|---|---|
| `CONFIG` | Hospital name, DB settings, admin password, time slots, booking window |
| `DB helpers` | Connection pool, `q()` query helper, table definitions, sample data |
| `LLM` | Loads the local Qwen model in a background thread |
| `Hospital knowledge` | Reads doctors / departments / free slots from the database |
| `Extract helpers` | Pulls phone, email, date, time, doctor, department out of free text |
| `BOOKING STATE MACHINE` | The chatbot brain — decides what to ask next and builds every reply |
| `FastAPI app` | Web routes: `/`, `/admin`, `/api/chat` |
| `Admin API` | Admin login + manage doctors / departments / appointments |

---

## 🧰 Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Check with `python --version` |
| **PostgreSQL** (running) | Any recent version; you need a user that can **create databases** (e.g. `postgres`) |
| **~2–3 GB free disk** | Only because of `torch` + the AI model download (see [AI model note](#-important-note-about-the-ai-model)) |
| **Internet (first run only)** | To install packages and download the model. After that the app works offline |

---

## 🚀 Setup & run (step by step)

### 1. Get the project

Unzip `hospital-booking.zip` and open a terminal inside the `hospital-booking` folder.

### 2. Create a virtual environment & activate it

**Windows (PowerShell / CMD):**
```bash
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
pip install python-dotenv
```

> ⚠️ `python-dotenv` is **used by `main.py` but missing from `requirements.txt`**. Without it the app crashes with `ModuleNotFoundError: No module named 'dotenv'`. Add the line `python-dotenv` to `requirements.txt` to fix this permanently.

### 4. Make sure PostgreSQL is running

Check that PostgreSQL is started on your machine (default port `5432`). You do **not** need to create the database manually — the app creates `hospital_db` automatically.

### 5. Configure the `.env` file

Open `.env` and fill in your values — see [Configuration](#-configuration-env) below. At minimum, check `DB_PASSWORD`, `ADMIN_PASSWORD` and `MODEL_DIR`.

### 6. Start the server

```bash
python main.py
```

You should see output similar to:

```
[model] Loading Qwen/Qwen2.5-0.5B-Instruct ...
[model] Ready on CPU.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

> On the **very first run** the model is downloaded (about 1 GB) — this can take a few minutes. The website works while this happens.

### 7. Open the app

| Page | URL |
|---|---|
| Patient chat | http://localhost:8000/ |
| Admin panel | http://localhost:8000/admin |

To use a different port: set an environment variable `PORT=9000` before running.

### What happens automatically on first start

1. Creates the `hospital_db` database if it doesn't exist
2. Creates all tables (only if missing)
3. Inserts **7 sample departments and 7 sample doctors** (only when the departments table is empty)
4. Starts loading the AI model in the background

---

## ⚙️ Configuration (`.env`)

All settings are read from the `.env` file next to `main.py`.

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=hospital_db
DB_USER=postgres
DB_PASSWORD=your_postgres_password
ADMIN_PASSWORD=your_admin_panel_password
MODEL_DIR=D:\hf_models
MODEL_ID=Qwen/Qwen2.5-0.5B-Instruct
```

| Variable | Meaning |
|---|---|
| `DB_HOST` / `DB_PORT` | Where PostgreSQL is running |
| `DB_NAME` | Database name (auto-created if missing) |
| `DB_USER` / `DB_PASSWORD` | PostgreSQL login |
| `ADMIN_PASSWORD` | Password for the `/admin` page |
| `MODEL_DIR` | Folder where the AI model is stored/downloaded. **Change this on any machine without a `D:` drive** (e.g. `./models` or `C:\hf_models`) |
| `MODEL_ID` | Hugging Face model name |
| `HOSPITAL_NAME` *(optional)* | Name shown in bot replies. Default: `City Care Hospital` |
| `PORT` *(optional)* | Web server port. Default: `8000` |

Hard-coded settings inside `main.py` (CONFIG section):

```python
SLOTS    = ["09:00","10:00","11:00","12:00","14:00","15:00","16:00","17:00"]  # daily slots
MAX_DAYS = 60                                                                # how far ahead patients can book
```

---

## 🧠 How it works (the logic)

### Big picture

```
 Browser (index.html)                         FastAPI (main.py)                    PostgreSQL
 ────────────────────                         ─────────────────                    ──────────
 user types message ──POST /api/chat──▶  chat() ──▶ chat_turn()  ──reads──▶  departments, doctors,
 (sends session_id + text)                         │                          appointments
                                                   ├─ decides reply            
                                                   ├─ on "Yes": do_insert() ──writes──▶ appointments
                                                   └─ save_session() ───────writes──▶ conversation_sessions
 shows reply + clickable chips ◀──── {reply, options} ────┘
```

**The chatbot is rule-based** ("state machine"), *not* AI-driven. Python code decides every step, so a booking can never be confirmed or saved by mistake. This makes it predictable and safe.

### Conversation state

Each browser tab gets a random **session id** (stored in `sessionStorage`). The server keeps one state object per session:

```python
{
  "state":   "idle" | "booking" | "confirm" | "done",
  "step":    "name" | "phone" | "email" | "dept" | "doctor" | "date" | "slot",
  "data":    { name, phone, email, dept_id, doctor_id, apt_date, slot, ... },
  "history": [ last 20 (user, bot) message pairs ]
}
```

The state is kept in memory **and** saved to the `conversation_sessions` table after every message, so refreshing the page or restarting the server doesn't lose progress.

### Booking steps (in order)

```
 name ─▶ phone ─▶ email ─▶ department ─▶ doctor ─▶ date ─▶ time slot ─▶ SUMMARY ─▶ Yes ─▶ saved ✅
                                                                                    └─▶ No  ─▶ cancelled
```

Each step has a **validator** (`validate_step`):

| Step | Rule |
|---|---|
| Name | Letters, spaces, `. ' -` only (filler like "my name is…" is stripped) |
| Phone | Indian 10-digit mobile starting with 6–9 (optional `+91`) |
| Email | Standard email pattern |
| Department | By list number, name, or keyword (e.g. "heart" → Cardiology) |
| Doctor | By list number or (partial) name; must be *available* |
| Date | `today`, `tomorrow`, `day after`, or `26 Oct 2026` / `2026-10-26`; not in the past; within `MAX_DAYS`; doctor must have free slots |
| Time slot | By list number or time (`10am`, `14:00`); nearest slot within 90 min is accepted |

If input is invalid, the bot shows an error message **and repeats the same question**.

### How `chat_turn()` decides a reply

For every message, the **first matching rule** wins (top to bottom):

1. Load the session (memory → database → new)
2. `__start__` (sent automatically on page load) → welcome / "Welcome back"
3. Empty message → ask for input
4. **Emergency words** → emergency warning (booking is not continued)
5. **Department change** → switch department, clear doctor/date/slot
6. **Change doctor / date / time** → clear that field and re-ask
7. **Info question** ("list doctors", "what departments…") → answer from DB, then re-ask the current booking question
8. Hi / thanks / cancel / restart handling
9. State `idle` → start booking if user asked to book; otherwise suggest a department or give a general reply
10. State `confirm` → handle Yes / No
11. Fill the current booking step (also **smart-skips**: if the user typed extra details like *"book cardiologist tomorrow, my number is 98…"*, those fields are filled and not asked again)
12. All fields filled → show summary and ask for confirmation

### Preventing double booking

Three layers of protection:

1. **`free_slots()`** only offers slots not already `booked` (and hides slots already passed today).
2. **`do_insert()`** runs in a database transaction, locks the doctor row (`FOR UPDATE`), and re-validates doctor, department, date and slot right before saving.
3. **Database unique indexes:**
   - `uq_doctor_slot` → one `booked` appointment per doctor + date + time
   - `uq_appointment_booking_key` → clicking "Yes" twice can't create two bookings

If someone else grabs the slot at the same moment, the bot replies *"That slot was just taken"* and shows the remaining slots.

### Minimum reply delay

`/api/chat` deliberately pads every response to **at least 9 seconds** (`time.sleep(...)`, marked `# ADDED` in code) so the typing indicator is visible. If replies feel slow, this is why — change or remove that line in `chat()`.

---

## 🗄️ Database design

Four tables, created automatically:

```
departments (id, name UNIQUE)
     ▲
     │ department_id
doctors (id, name, department_id, qualification, experience_years, details, available, created_at)
     ▲
     │ doctor_id
appointments (id, patient_name, phone, email, doctor_id, appointment_date, time_slot,
              status, booking_key UNIQUE, created_at)

conversation_sessions (session_id PK, payload JSONB, updated_at)    ← chat state, no foreign keys
```

- `appointments.status` is one of: `booked`, `completed`, `cancelled`
- A **cancelled/completed** appointment frees its slot again (the unique index only applies to `status = 'booked'`)
- `doctors.available = false` hides the doctor from patients but keeps history

**Sample data** (inserted on first run only): General Medicine, Cardiology, Orthopedics, Pediatrics, Dermatology, ENT, Gynecology — one doctor in each.

---

## 🔌 API reference

### Public

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Patient chat page |
| GET | `/admin` | Admin page |
| POST | `/api/chat` | Send a message. Body: `{"session_id": "...", "message": "..."}` → returns `{"reply": "...", "options": [...]}` |
| GET | `/api/llm-status` | AI model status (`starting / downloading / loading / ready / error`) — drives the status dot in the chat header |

### Admin (requires header `X-Admin-Token: <token>`)

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/admin/login` | Body `{"password": "..."}` → returns `{"token": "..."}` |
| GET | `/api/admin/departments` | List departments (with doctor counts) |
| POST | `/api/admin/departments` | Add department |
| DELETE | `/api/admin/departments/{id}` | Delete (blocked if it still has doctors) |
| GET | `/api/admin/doctors` | List doctors |
| POST | `/api/admin/doctors` | Add doctor |
| PUT | `/api/admin/doctors/{id}` | Edit doctor |
| PATCH | `/api/admin/doctors/{id}/availability` | Mark available / unavailable |
| DELETE | `/api/admin/doctors/{id}` | Delete (blocked if the doctor has appointments) |
| GET | `/api/admin/appointments?date=YYYY-MM-DD&status=booked` | List/filter bookings |
| PATCH | `/api/admin/appointments/{id}` | Change status (`booked` / `completed` / `cancelled`) |

Interactive API docs are also available (FastAPI built-in) at **http://localhost:8000/docs**.

---

## 👩‍⚕️ Admin panel guide

1. Go to http://localhost:8000/admin and log in with `ADMIN_PASSWORD` from `.env`.
2. **Doctors tab** — add a doctor, or use the row buttons: *Mark unavailable/available*, *Edit*, *Delete*.
3. **Bookings tab** — filter by date and status; change a booking's status from the dropdown in its row.
4. **Departments tab** — add or delete departments.

Notes:
- A department can only be deleted when it has **no doctors**.
- A doctor who has appointments **cannot be deleted** — mark them *unavailable* instead.
- The admin login token is stored in memory on the server, so **restarting the server logs everyone out**.

---

## 🤖 Important note about the AI model

`main.py` loads **Qwen2.5-0.5B-Instruct** in a background thread at startup, and the chat header shows a status dot for it. However:

> The function `llm_answer()` that talks to the model is **defined but never called**. Every reply the bot gives today is **scripted** (rule-based + database lookups).

So currently the model only costs **disk space (~1 GB), RAM, and startup time** without changing the chatbot's behaviour. Options:

- **Keep it** if you plan to use `llm_answer()` for free-form questions in the future.
- **Remove it** to make the project much lighter: delete `torch`, `transformers`, `safetensors`, `numpy`, `huggingface_hub` from `requirements.txt`, remove the `threading.Thread(target=load_llm...)` line in `lifespan()`, and drop `MODEL_DIR` / `MODEL_ID`. (`llm_answer` and the model code can then be deleted, and the status dot in `index.html` removed.)

---

## ⚠️ Known issues & security notes

Please read before deploying or sharing the project.

1. **`.env` contains real passwords.** Never commit it or send it to others. Create a `.gitignore` with:
   ```
   .env
   venv/
   __pycache__/
   ```
   If the zip was already shared, **change the DB and admin passwords**.
2. **`python-dotenv` missing from `requirements.txt`** (see Setup step 3).
3. **`MODEL_DIR` points to `D:\hf_models`** — fails on machines without a `D:` drive. Change it in `.env`.
4. **The server binds to `127.0.0.1`** (local machine only). To make it reachable on a network, change `host` in the last lines of `main.py` — and add HTTPS and stronger auth first.
5. **Admin security is basic:** one shared password, tokens stored in memory, no expiry, no login-attempt limit. Fine for a demo/college project; not enough for production.
6. **Patients are not verified** — anyone can book with any phone/email. There is no OTP/email confirmation and no way for a patient to cancel by themselves (admin must cancel).
7. **Doctor schedules are fixed:** every doctor offers the same 8 daily slots, 7 days a week (no days off, no per-doctor timings).
8. **Booking window:** today up to `MAX_DAYS` (60) days ahead.
9. **Unused code in `main.py`:** `llm_answer`, `extract_name`, `BOOKING_STEPS` are defined but never used.
10. **No automated tests** are included.

---

## 🛠️ Troubleshooting

| Problem | Cause / Fix |
|---|---|
| `ModuleNotFoundError: No module named 'dotenv'` | `pip install python-dotenv` |
| `KeyError: 'DB_HOST'` (or other var) | `.env` is missing, in the wrong folder (must be next to `main.py`), or a variable is missing |
| `[db] Cannot connect to PostgreSQL` | PostgreSQL isn't running, or `DB_HOST/PORT/USER/PASSWORD` in `.env` are wrong |
| `permission denied to create database` | The DB user needs the `CREATEDB` right, or create `hospital_db` manually first |
| Status dot stays yellow / model "downloading" | First-run download in progress; wait a few minutes. The chat still works |
| Model status = `error` | Check internet, free disk space, and that `MODEL_DIR` is a valid, writable path. The chatbot still works without the model |
| Chat replies take ~9 seconds | Intentional delay in `chat()` — see [Minimum reply delay](#minimum-reply-delay) |
| "Please log in as admin." | Token expired (server restarted). Log in again |
| Port 8000 already in use | Set a different port: `PORT=9000 python main.py` (Windows PowerShell: `$env:PORT=9000; python main.py`) |
| Bot doesn't show a doctor | Doctor is marked *unavailable*, or their department has no available doctors |

---

## 🎨 How to customise

| I want to… | Change this |
|---|---|
| Rename the hospital | `HOSPITAL_NAME` in `.env` (and the hard-coded text in `index.html`, plus the goodbye message in `handle_general_message`) |
| Change appointment times | `SLOTS` list in `main.py` |
| Allow booking further ahead | `MAX_DAYS` in `main.py` |
| Add keywords for a department | `DEPT_ALIASES` dictionary in `main.py` |
| Add emergency keywords | `EMERGENCY_RE` in `main.py` |
| Change sample doctors/departments | `SEED_DEPARTMENTS` / `SEED_DOCTORS` (only used on a **fresh, empty** database) |
| Make replies faster | Remove the `time.sleep(...)` line in `chat()` |
| Change the look of the chat | CSS in `static/index.html` |
| Reset everything | Drop the `hospital_db` database (it will be recreated with sample data on next start) |

---

## 📌 Quick start (TL;DR)

```bash
python -m venv venv
venv\Scripts\activate              # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
pip install python-dotenv
# edit .env  (DB_PASSWORD, ADMIN_PASSWORD, MODEL_DIR)
python main.py
# open http://localhost:8000  and  http://localhost:8000/admin
```
