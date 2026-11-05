-- Shared settings and connection definitions of one management root (global/settings.db).
-- Versioned independently of project databases via PRAGMA user_version. Past revisions,
-- connection versions and events are immutable. No secret values are stored here.
CREATE TABLE IF NOT EXISTS global_settings_versions(
 revision INTEGER PRIMARY KEY CHECK(revision>0),
 content_json TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS global_connection_versions(
 connection_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 content_json TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(connection_id,version));
CREATE TABLE IF NOT EXISTS global_connection_state(
 connection_id TEXT PRIMARY KEY,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 state_revision INTEGER NOT NULL CHECK(state_revision>0));
CREATE TABLE IF NOT EXISTS global_events(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
 payload_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS global_events_time ON global_events(created_at,id);
CREATE TRIGGER IF NOT EXISTS immutable_global_settings BEFORE UPDATE ON global_settings_versions
 BEGIN SELECT RAISE(ABORT,'immutable global settings revision'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_settings_delete BEFORE DELETE ON global_settings_versions
 BEGIN SELECT RAISE(ABORT,'immutable global settings revision'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_connections BEFORE UPDATE ON global_connection_versions
 BEGIN SELECT RAISE(ABORT,'immutable global connection version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_connections_delete BEFORE DELETE ON global_connection_versions
 BEGIN SELECT RAISE(ABORT,'immutable global connection version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_events BEFORE UPDATE ON global_events
 BEGIN SELECT RAISE(ABORT,'immutable global event'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_events_delete BEFORE DELETE ON global_events
 BEGIN SELECT RAISE(ABORT,'immutable global event'); END;
-- v2 (additive): AI tools shared by every project, versioned apart from shared defaults so a
-- save on one screen never drops the other. Login state is status, not a settings revision.
CREATE TABLE IF NOT EXISTS global_ai_versions(
 revision INTEGER PRIMARY KEY CHECK(revision>0),
 content_json TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS global_ai_auth(
 provider TEXT PRIMARY KEY,
 authenticated INTEGER NOT NULL CHECK(authenticated IN (0,1)),
 checked_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_global_ai BEFORE UPDATE ON global_ai_versions
 BEGIN SELECT RAISE(ABORT,'immutable global AI revision'); END;
CREATE TRIGGER IF NOT EXISTS immutable_global_ai_delete BEFORE DELETE ON global_ai_versions
 BEGIN SELECT RAISE(ABORT,'immutable global AI revision'); END;
