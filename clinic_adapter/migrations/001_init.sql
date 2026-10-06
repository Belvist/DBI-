-- 001_init: base clinic schema (idempotent; safe to re-apply).
CREATE TABLE IF NOT EXISTS doctors(
  doctor_id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  short_name TEXT NOT NULL,
  specialty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
  slot_id TEXT PRIMARY KEY,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  taken_by TEXT NULL
);
CREATE TABLE IF NOT EXISTS appointments(
  booking_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  slot_id TEXT NOT NULL,
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_doc_start ON slots(doctor_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, status);
