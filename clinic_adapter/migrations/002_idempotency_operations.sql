-- 002_idempotency_operations: scoped idempotency, per patient+kind+key.
-- Replaces the old global UNIQUE(idempotency_key) which leaked results
-- across patients and across operation kinds.
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_idempotency_key_key;
CREATE TABLE IF NOT EXISTS idempotency_operations(
  patient_id TEXT NOT NULL,
  operation_kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_booking_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (patient_id, operation_kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idem_result ON idempotency_operations(result_booking_id);
