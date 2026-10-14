-- SPDX-License-Identifier: MIT
-- The worker store's baseline, version 1 (schema.py). Never edit it: stores made with it
-- keep it. Change the schema by appending a step to schema.MIGRATIONS.

-- Executions, their events and recovery (store.py, journal.py, recovery.py).
CREATE TABLE executions (
    execution_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    terminal INTEGER NOT NULL,
    record TEXT NOT NULL,
    kernel_scope TEXT NOT NULL DEFAULT 'legacy'
);
CREATE INDEX executions_session ON executions (environment_id, session_id);
CREATE TABLE events (
    execution_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (execution_id, seq)
);
CREATE TABLE kernels (
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    language TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    base INTEGER NOT NULL,
    last INTEGER NOT NULL,
    PRIMARY KEY (environment_id, session_id, language)
);
CREATE TABLE environment_revisions (
    environment_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE TABLE revision_entries (
    environment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    recorded REAL NOT NULL,
    lockfile TEXT,
    lockfile_size INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (environment_id, revision)
);
CREATE INDEX revision_entries_lockfile ON revision_entries (lockfile);
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
CREATE TABLE artifact_capture_leases (
    publication_id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    guest_clean INTEGER NOT NULL,
    sandbox TEXT NOT NULL,
    sandbox_id TEXT NOT NULL
);
CREATE UNIQUE INDEX artifact_capture_helper
ON artifact_capture_leases(capture_id) WHERE capture_id != '';
CREATE TABLE provisions (
    provision_id TEXT PRIMARY KEY,
    operation_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    finished INTEGER NOT NULL,
    submitted REAL NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX provisions_environment ON provisions (environment_id, finished);
CREATE TABLE executor_endpoints (
    environment_id TEXT PRIMARY KEY,
    incarnation TEXT NOT NULL,
    sandbox TEXT NOT NULL DEFAULT '',
    sandbox_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE executor_recycles (
    environment_id TEXT PRIMARY KEY,
    incarnation TEXT NOT NULL,
    sandbox TEXT NOT NULL DEFAULT '',
    sandbox_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE execution_recovery (
    execution_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    incarnation TEXT NOT NULL DEFAULT '',
    source_seq INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE package_status (
    environment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    provision_id TEXT NOT NULL,
    revision INTEGER
);
CREATE TABLE package_execution_gates (
    environment_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    mode TEXT NOT NULL,
    operation TEXT NOT NULL,
    revision INTEGER,
    binding TEXT NOT NULL DEFAULT ''
);
CREATE TABLE provision_steps (
    provision_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    PRIMARY KEY(provision_id, operation_id)
);

-- Kernel start revisions and package epochs (kernel_revisions.py).
CREATE TABLE kernel_start_revisions (
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    language TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (environment_id, session_id, language)
);
CREATE TABLE kernel_package_epochs (
    environment_id TEXT PRIMARY KEY,
    incarnation TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL
);

-- Worker-local output observations (output_observations.py).
CREATE TABLE output_observation_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation BLOB NOT NULL, baseline BLOB NOT NULL
);
CREATE TABLE output_observation_scopes (
 project TEXT NOT NULL, session TEXT NOT NULL, environment TEXT NOT NULL,
 head INTEGER NOT NULL, evicted_through INTEGER NOT NULL, unavailable INTEGER NOT NULL,
 PRIMARY KEY(project,session,environment)
);
CREATE TABLE output_observation_attempts (
 execution TEXT PRIMARY KEY, project TEXT NOT NULL, session TEXT NOT NULL,
 environment TEXT NOT NULL,
 binding BLOB NOT NULL, seq INTEGER, marker BLOB NOT NULL DEFAULT X'',
 UNIQUE(project,session,environment,seq)
);
CREATE TABLE output_observation_details (
 execution TEXT PRIMARY KEY, payload BLOB NOT NULL, size INTEGER NOT NULL
);
CREATE INDEX output_observation_scope
 ON output_observation_attempts(project,session,environment,seq);
