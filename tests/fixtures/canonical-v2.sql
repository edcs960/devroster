CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_settings(id INTEGER PRIMARY KEY CHECK(id=1),
 revision INTEGER NOT NULL CHECK(revision>0), settings_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, topic TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','planning','active','review_pending','completed','archived')), version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0), created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_profile_versions(project_id TEXT REFERENCES projects(id),
 version INTEGER NOT NULL, digest TEXT NOT NULL, content_json TEXT NOT NULL,
 PRIMARY KEY(project_id,version));
CREATE TABLE IF NOT EXISTS repository_bindings(id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL REFERENCES projects(id), role TEXT NOT NULL, content_json TEXT NOT NULL,
 UNIQUE(project_id,role));
CREATE TABLE IF NOT EXISTS design_documents(id TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
 version INTEGER NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, target_type TEXT NOT NULL,
 target_id TEXT NOT NULL, target_version INTEGER NOT NULL, digest TEXT NOT NULL,
 actor TEXT NOT NULL, state TEXT NOT NULL, decided_at REAL NOT NULL,
 UNIQUE(target_type,target_id,target_version,actor));
CREATE TABLE IF NOT EXISTS task_plans(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
 design_id TEXT NOT NULL, design_version INTEGER NOT NULL, version INTEGER NOT NULL,
 digest TEXT NOT NULL, state TEXT NOT NULL,
 FOREIGN KEY(design_id,design_version) REFERENCES design_documents(id,version));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 plan_id TEXT REFERENCES task_plans(id), title TEXT NOT NULL, priority TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('planned','approval_pending','ready','running','blocked','waiting_for_capacity','verifying','pr_ready','user_review','feedback_required','completed','cancelled')), version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0), definition_json TEXT NOT NULL,
 snapshot_id TEXT REFERENCES input_snapshots(id), generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_dependencies(task_id TEXT REFERENCES tasks(id),
 depends_on_task_id TEXT REFERENCES tasks(id), PRIMARY KEY(task_id,depends_on_task_id),
 CHECK(task_id != depends_on_task_id));
CREATE TABLE IF NOT EXISTS input_snapshots(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 digest TEXT NOT NULL, resolved_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_snapshots BEFORE UPDATE ON input_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER IF NOT EXISTS immutable_profiles BEFORE UPDATE ON project_profile_versions
 BEGIN SELECT RAISE(ABORT,'immutable profile'); END;
CREATE TRIGGER IF NOT EXISTS immutable_design BEFORE UPDATE OF digest,content,project_id ON design_documents
 BEGIN SELECT RAISE(ABORT,'immutable design'); END;
CREATE TRIGGER IF NOT EXISTS immutable_plan BEFORE UPDATE OF digest,design_id,design_version,version ON task_plans
 BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
CREATE TABLE IF NOT EXISTS leases(resource TEXT PRIMARY KEY, holder TEXT NOT NULL,
 token INTEGER NOT NULL CHECK(token>0), expires_at REAL NOT NULL, heartbeat_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS phase_attempts(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 phase_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','running','passed','failed','interrupted','superseded','waiting_for_capacity')), idempotency_key TEXT NOT NULL, lease_resource TEXT NOT NULL,
 lease_token INTEGER NOT NULL, started_at REAL NOT NULL, ended_at REAL,
 UNIQUE(task_id,phase_id,attempt_no));
CREATE INDEX IF NOT EXISTS attempts_resume ON phase_attempts(task_id,phase_id,generation,state);
CREATE UNIQUE INDEX IF NOT EXISTS attempt_idempotency ON phase_attempts(idempotency_key)
 WHERE state IN ('running','passed');
CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 fingerprint TEXT NOT NULL, severity TEXT NOT NULL, owner_phase TEXT NOT NULL,
 state TEXT NOT NULL, evidence TEXT NOT NULL, retries INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS open_fingerprint ON findings(task_id,fingerprint) WHERE state='open';
CREATE TABLE IF NOT EXISTS feedback_tasks(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
 source_task_id TEXT REFERENCES tasks(id), fingerprint TEXT NOT NULL, content_json TEXT NOT NULL,
 state TEXT NOT NULL, UNIQUE(source_task_id,fingerprint));
CREATE TABLE IF NOT EXISTS tool_connections(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
 content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
 task_id TEXT REFERENCES tasks(id), attempt_id TEXT REFERENCES phase_attempts(id),
 path TEXT NOT NULL, digest TEXT NOT NULL, kind TEXT NOT NULL, trust TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(task_id,path,digest));
CREATE TABLE IF NOT EXISTS verification_runs(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 state TEXT NOT NULL, policy_digest TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS verification_results(id TEXT PRIMARY KEY,
 run_id TEXT REFERENCES verification_runs(id), gate TEXT NOT NULL, passed INTEGER NOT NULL,
 required INTEGER NOT NULL, exit_code INTEGER, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publish_records(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 repository TEXT NOT NULL, branch TEXT NOT NULL, commit_sha TEXT, pr_number TEXT,
 ci_state TEXT NOT NULL, merge_state TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS knowledge_candidates(id TEXT PRIMARY KEY,
 project_id TEXT REFERENCES projects(id), source_path TEXT NOT NULL, source_digest TEXT NOT NULL,
 source_commit TEXT NOT NULL, content TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL,
 version INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(aggregate_id,version));
CREATE INDEX IF NOT EXISTS event_projection ON events(aggregate_id,version);
CREATE TABLE IF NOT EXISTS notification_outbox(id TEXT PRIMARY KEY, event_id TEXT REFERENCES events(id),
 channel TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL, claim_until REAL NOT NULL DEFAULT 0,
 UNIQUE(event_id,channel));
CREATE TABLE IF NOT EXISTS commands(key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
 state TEXT NOT NULL, result_json TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capacity(connection_id TEXT PRIMARY KEY, ready_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_sources(path TEXT PRIMARY KEY, digest TEXT NOT NULL,
 content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY, compared_at REAL NOT NULL,
 zero_diff INTEGER NOT NULL, rollback_passed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_projections(source_path TEXT REFERENCES legacy_sources(path),
 kind TEXT NOT NULL, record_id TEXT NOT NULL, task_id TEXT,
 content_json TEXT NOT NULL, PRIMARY KEY(source_path,kind,record_id));
CREATE INDEX IF NOT EXISTS legacy_projection_owner ON legacy_projections(task_id,kind);

CREATE TRIGGER IF NOT EXISTS immutable_terminal_attempt BEFORE UPDATE ON phase_attempts
 WHEN OLD.state IN ('passed','failed','interrupted','superseded','waiting_for_capacity')
 BEGIN SELECT RAISE(ABORT,'terminal phase attempt is immutable'); END;
CREATE TABLE IF NOT EXISTS capability_grants(id TEXT PRIMARY KEY, connection_id TEXT REFERENCES tool_connections(id),
 project_id TEXT REFERENCES projects(id), task_id TEXT REFERENCES tasks(id), phase_id TEXT NOT NULL,
 capability TEXT NOT NULL, scope TEXT NOT NULL, actor TEXT NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('approved','consumed')));

CREATE INDEX IF NOT EXISTS task_scheduling ON tasks(project_id,state,priority);
CREATE INDEX IF NOT EXISTS artifact_owner ON artifacts(task_id,created_at);
CREATE INDEX IF NOT EXISTS outbox_schedule ON notification_outbox(channel,state,next_attempt_at);

CREATE TABLE IF NOT EXISTS workflow_definitions(project_id TEXT NOT NULL REFERENCES projects(id),
 id TEXT NOT NULL, name TEXT NOT NULL, current_version INTEGER NOT NULL CHECK(current_version>0),
 PRIMARY KEY(project_id,id));
CREATE TABLE IF NOT EXISTS workflow_versions(project_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), digest TEXT NOT NULL, document_json TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(project_id,workflow_id,version),
 FOREIGN KEY(project_id,workflow_id) REFERENCES workflow_definitions(project_id,id));
CREATE TRIGGER IF NOT EXISTS immutable_workflow_versions BEFORE UPDATE ON workflow_versions
 BEGIN SELECT RAISE(ABORT,'immutable workflow version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_workflow_versions_delete BEFORE DELETE ON workflow_versions
 BEGIN SELECT RAISE(ABORT,'immutable workflow version'); END;
CREATE TABLE IF NOT EXISTS workflow_node_runs(task_id TEXT NOT NULL REFERENCES tasks(id),
 node_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
 state TEXT NOT NULL CHECK(state IN ('pending','running','passed','skipped','failed','awaiting_approval','rejected','interrupted')),
 input_digest TEXT NOT NULL, result_json TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>=0),
 PRIMARY KEY(task_id,node_id,generation));
CREATE INDEX IF NOT EXISTS workflow_node_resume ON workflow_node_runs(task_id,generation,state);
CREATE TABLE IF NOT EXISTS workflow_node_approvals(id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL, node_id TEXT NOT NULL, generation INTEGER NOT NULL,
 snapshot_digest TEXT NOT NULL, input_digest TEXT NOT NULL, actor TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')), created_at REAL NOT NULL,
 UNIQUE(task_id,node_id,generation,snapshot_digest,input_digest,actor),
 FOREIGN KEY(task_id,node_id,generation) REFERENCES workflow_node_runs(task_id,node_id,generation));
