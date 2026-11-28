CREATE TABLE IF NOT EXISTS slots (
  slot_key TEXT PRIMARY KEY,
  target TEXT NOT NULL,
  due INTEGER NOT NULL,
  deadline INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  run_created_at TEXT,
  job_started_at TEXT,
  conclusion TEXT,
  receipt TEXT,
  last_error TEXT,
  last_checked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS slots_due ON slots(due);
CREATE TABLE IF NOT EXISTS health (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  last_tick INTEGER NOT NULL
);
