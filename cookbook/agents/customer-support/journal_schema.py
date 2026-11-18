"""SQLite execution journal schema; existing schema bytes are preserved."""

from local import DEPLOYMENT_SQL

RECOVERY_SQL = (
    DEPLOYMENT_SQL
    + """
CREATE TABLE threads (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, case_id TEXT NOT NULL,
 run_id TEXT, run_started_at TEXT,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','waiting','waiting_customer','completed','reconciliation_required')),
 checkpoint_id TEXT, run_ended_at TEXT, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls>=0), active_command_id TEXT,
 read_calls INTEGER NOT NULL DEFAULT 0 CHECK(read_calls>=0),
 observation_incomplete INTEGER NOT NULL DEFAULT 0 CHECK(observation_incomplete IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(deployment_id,id), UNIQUE(deployment_id,run_id),
 FOREIGN KEY(deployment_id,active_command_id) REFERENCES ui_commands(deployment_id,id) DEFERRABLE INITIALLY DEFERRED);
CREATE UNIQUE INDEX unfinished_case ON threads(deployment_id,case_id) WHERE status!='completed';
CREATE TABLE ui_commands (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, thread_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('start','reply','resume')), request_json TEXT NOT NULL CHECK(json_valid(request_json)),
 status TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','running','waiting','completed','failed','reconciliation_required')),
 reason_code TEXT, recover_command_id TEXT, input_recorded INTEGER NOT NULL DEFAULT 0 CHECK(input_recorded IN (0,1)),
 input_checkpoint_id TEXT, output_checkpoint_id TEXT, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(deployment_id,id), FOREIGN KEY(deployment_id,thread_id) REFERENCES threads(deployment_id,id),
 FOREIGN KEY(deployment_id,recover_command_id) REFERENCES ui_commands(deployment_id,id),
 CHECK(recover_command_id IS NULL OR (kind='resume' AND recover_command_id!=id)));
CREATE TABLE attempts (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), decision_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, tool_id TEXT NOT NULL, tool_version_id TEXT NOT NULL,
 policy_contract_set_digest TEXT NOT NULL, request_format TEXT NOT NULL CHECK(request_format='decision-request-v3'), source_time TEXT NOT NULL, event_id TEXT NOT NULL,
 native_call_json TEXT NOT NULL CHECK(json_valid(native_call_json)), decision_input_json TEXT NOT NULL CHECK(json_valid(decision_input_json)),
 event_template_json TEXT NOT NULL CHECK(json_valid(event_template_json)), terminal_event_json TEXT,
 pause_event_json TEXT CHECK(pause_event_json IS NULL OR json_valid(pause_event_json)),
 resume_event_json TEXT CHECK(resume_event_json IS NULL OR json_valid(resume_event_json)),
 idempotency_key TEXT, status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','waiting','executing','succeeded','failed','blocked','reconciliation_required')),
 result_json TEXT, reason_code TEXT, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(deployment_id,decision_id), UNIQUE(deployment_id,thread_id,tool_call_id), UNIQUE(deployment_id,event_id),
 FOREIGN KEY(deployment_id,thread_id) REFERENCES threads(deployment_id,id));
CREATE TABLE outbox (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, thread_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('run_active','event','run_complete')), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','acknowledged')), created_at TEXT NOT NULL,
 PRIMARY KEY(deployment_id,id), FOREIGN KEY(deployment_id,thread_id) REFERENCES threads(deployment_id,id));
CREATE TABLE conversation_turns (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, thread_id TEXT NOT NULL, command_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0), speaker TEXT NOT NULL CHECK(speaker IN ('customer','agent')),
 body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=8192), created_at TEXT NOT NULL,
 PRIMARY KEY(deployment_id,id), UNIQUE(deployment_id,command_id,ordinal),
 FOREIGN KEY(deployment_id,thread_id) REFERENCES threads(deployment_id,id),
 FOREIGN KEY(deployment_id,command_id) REFERENCES ui_commands(deployment_id,id));
"""
)

RECOVERY_SQL += """
CREATE TABLE local_attempts (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, thread_id TEXT NOT NULL,
 tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, native_call_json TEXT NOT NULL CHECK(json_valid(native_call_json)),
 idempotency_key TEXT, status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','executing','succeeded','failed','reconciliation_required')),
 result_json TEXT, reason_code TEXT, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(deployment_id,id),
 UNIQUE(deployment_id,thread_id,tool_call_id), UNIQUE(deployment_id,idempotency_key),
 FOREIGN KEY(deployment_id,thread_id) REFERENCES threads(deployment_id,id));
CREATE TABLE catalog_versions (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), tool_name TEXT NOT NULL,
 schema_json TEXT NOT NULL CHECK(json_valid(schema_json)), tool_id TEXT NOT NULL, tool_version_id TEXT NOT NULL,
 tool_contract_digest TEXT, observed_at TEXT NOT NULL, PRIMARY KEY(deployment_id,tool_name,tool_version_id));
"""
