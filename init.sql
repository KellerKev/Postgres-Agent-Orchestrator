-- Extensions (pgmq is already created by install_pgmq.sh)
CREATE EXTENSION IF NOT EXISTS ltree;

-- Task queue
SELECT pgmq.create('tasks');

-- Agent memory table — stores every agent's input/output with lineage tracking
CREATE TABLE IF NOT EXISTS agent_memory (
  id          SERIAL PRIMARY KEY,
  agent_id    TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  parent_id   TEXT,
  lineage     ltree,
  input       JSONB,
  output      JSONB,
  status      TEXT DEFAULT 'pending',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_lineage ON agent_memory USING GIST (lineage);
CREATE INDEX IF NOT EXISTS idx_memory_task    ON agent_memory (task_id);
CREATE INDEX IF NOT EXISTS idx_memory_status  ON agent_memory (status);

-- Fire NOTIFY when any agent finishes work — the summarizer listens for this
-- Fires on both INSERT and UPDATE so direct inserts with status='done' also trigger
CREATE OR REPLACE FUNCTION notify_agent_wakeup()
RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'done' THEN
    PERFORM pg_notify('agent_wakeup', row_to_json(NEW)::text);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_memory_notify ON agent_memory;
CREATE TRIGGER agent_memory_notify
AFTER INSERT OR UPDATE ON agent_memory
FOR EACH ROW EXECUTE FUNCTION notify_agent_wakeup();

-- Seed 3 tasks so the demo works immediately
SELECT pgmq.send('tasks', '{"topic": "artificial intelligence"}');
SELECT pgmq.send('tasks', '{"topic": "PostgreSQL internals"}');
SELECT pgmq.send('tasks', '{"topic": "data sovereignty in Europe"}');
