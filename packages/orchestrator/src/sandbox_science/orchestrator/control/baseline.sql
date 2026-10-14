-- SPDX-License-Identifier: MIT
-- The control store's schema at version 2, the baseline that migrations.py starts from.
-- Never edit this file: a database made from it keeps it. A change to the schema is a
-- new step at the end of MIGRATIONS in migrations.py.

-- Documents, revisions, patches, operation IDs and events (documents/store.py)
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_session TEXT,
    takes_suggestions INTEGER NOT NULL DEFAULT 0,
    head INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    next_block INTEGER NOT NULL,
    next_span INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX documents_names
    ON documents (project_id, coalesce(session_id, ''), name) WHERE deleted = 0;
CREATE TABLE revisions (
    document_id TEXT NOT NULL REFERENCES documents (document_id),
    number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    accepted_by TEXT,
    session_id TEXT,
    base INTEGER,
    parent INTEGER,
    reason TEXT NOT NULL,
    operations TEXT NOT NULL,
    patch_id TEXT,
    text TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    normalized TEXT NOT NULL,
    warnings TEXT NOT NULL,
    block_map TEXT NOT NULL,
    parser TEXT NOT NULL,
    PRIMARY KEY (document_id, number)
) STRICT;
CREATE TABLE patches (
    patch_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents (document_id),
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    session_id TEXT,
    base INTEGER NOT NULL,
    reason TEXT NOT NULL,
    operations TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER,
    details TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
) STRICT;
CREATE INDEX patches_documents ON patches (document_id, created_at);
CREATE TABLE requests (
    project_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, actor, operation_id)
) STRICT;
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    kind TEXT NOT NULL,
    producer TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_id TEXT,
    document_id TEXT NOT NULL,
    payload TEXT NOT NULL
) STRICT;
CREATE INDEX events_projects ON events (project_id, seq);
CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions
BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END;
CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions
BEGIN SELECT RAISE(ABORT, 'revisions are append-only'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Comments (documents/annotations.py)
CREATE TABLE annotation_threads (
    thread_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
    anchor TEXT NOT NULL, opener TEXT NOT NULL, created_at REAL NOT NULL) STRICT;
CREATE TABLE annotation_entries (
    entry_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES annotation_threads(thread_id),
    number INTEGER NOT NULL, kind TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
    payload TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(thread_id, number)) STRICT;
CREATE TABLE annotation_operations (
    project_id TEXT NOT NULL, actor TEXT NOT NULL, operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(project_id,actor,operation_id)) STRICT;
CREATE TABLE annotation_requests (
    request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES annotation_threads(thread_id),
    entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id), state TEXT NOT NULL,
    claimed_run TEXT, claim_until REAL, delivered_run TEXT, delivered_at REAL,
    responded_entry TEXT, UNIQUE(entry_id)) STRICT;
CREATE INDEX annotation_pending ON annotation_requests(project_id,session_id,state);
CREATE TRIGGER annotation_threads_update BEFORE UPDATE ON annotation_threads
BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER annotation_threads_delete BEFORE DELETE ON annotation_threads
BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER annotation_entries_update BEFORE UPDATE ON annotation_entries
BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER annotation_entries_delete BEFORE DELETE ON annotation_entries
BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;

-- Math checks of documents (documents/mathqueue.py, mathjobs.py)
CREATE TABLE math_sources (
    document TEXT PRIMARY KEY REFERENCES documents(document_id), project TEXT NOT NULL,
    target INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
    blocked TEXT, retry_requested INTEGER NOT NULL DEFAULT 0) STRICT;
CREATE TABLE math_block_owners (
    document TEXT NOT NULL REFERENCES documents(document_id), block TEXT NOT NULL,
    revision INTEGER NOT NULL, author TEXT NOT NULL,
    PRIMARY KEY(document,block)) STRICT;
CREATE TABLE math_jobs (
    check_id TEXT PRIMARY KEY, document TEXT REFERENCES documents(document_id),
    revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL, state TEXT NOT NULL,
    owner TEXT, lease_until REAL, cursor INTEGER NOT NULL DEFAULT 0,
    spans INTEGER, available INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
    unavailable INTEGER NOT NULL DEFAULT 0, configuration TEXT, runtime TEXT, reason TEXT,
    attribution_cursor INTEGER, created REAL NOT NULL, ended REAL,
    CHECK(revision>0)) STRICT;
CREATE INDEX math_revision ON math_jobs(document,revision);
CREATE UNIQUE INDEX math_one_running
    ON math_jobs(document) WHERE state='running';
CREATE TABLE math_observations (
    check_id TEXT NOT NULL REFERENCES math_jobs(check_id), ordinal INTEGER NOT NULL,
    block TEXT NOT NULL, span TEXT NOT NULL, input_sha256 TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL,
    PRIMARY KEY(check_id,ordinal)) STRICT;
CREATE INDEX math_observation_span ON math_observations(block,span,check_id);
CREATE TABLE math_notices (
    finding_id TEXT NOT NULL, revision INTEGER NOT NULL, project TEXT NOT NULL,
    session TEXT NOT NULL, authored_run TEXT, claimed_run TEXT, claim_until REAL,
    delivered_run TEXT, PRIMARY KEY(finding_id,revision)) STRICT;
CREATE TABLE math_job_owners (
    check_id TEXT NOT NULL REFERENCES math_jobs(check_id),block TEXT NOT NULL,
    revision INTEGER,author TEXT,bytes INTEGER NOT NULL,PRIMARY KEY(check_id,block)) STRICT;
CREATE TABLE math_finding_state (
    finding_id TEXT PRIMARY KEY REFERENCES review_findings(finding_id),
    document TEXT NOT NULL,block TEXT NOT NULL,span TEXT NOT NULL,
    checked_revision INTEGER NOT NULL,input_sha256 TEXT NOT NULL,
    owner_revision INTEGER NOT NULL,
    checked_check TEXT NOT NULL REFERENCES math_jobs(check_id),excluded TEXT,
    UNIQUE(document,block,span)) STRICT;
CREATE TABLE math_ignored (
    finding_id TEXT NOT NULL, revision INTEGER NOT NULL, run TEXT NOT NULL,
    PRIMARY KEY(finding_id,revision)) STRICT;
CREATE TRIGGER math_observations_update BEFORE UPDATE ON math_observations
BEGIN SELECT RAISE(ABORT,'math observations are immutable'); END;
CREATE TRIGGER math_observations_delete BEFORE DELETE ON math_observations
BEGIN SELECT RAISE(ABORT,'math observations are immutable'); END;

-- Project macros and the macro context of each check (documents/project_macros.py)
CREATE TABLE project_macro_revisions (
    project TEXT NOT NULL,revision INTEGER NOT NULL,definitions TEXT NOT NULL,
    sha256 TEXT NOT NULL,author TEXT NOT NULL,PRIMARY KEY(project,revision)) STRICT;
CREATE TABLE project_macro_heads (
    project TEXT PRIMARY KEY,revision INTEGER NOT NULL) STRICT;
CREATE TABLE project_macro_operations (
    project TEXT NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,revision INTEGER NOT NULL,
    PRIMARY KEY(project,actor,operation)) STRICT;
CREATE TABLE math_macro_pins (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),project TEXT NOT NULL,
    revision INTEGER NOT NULL,sha256 TEXT NOT NULL,anchors TEXT NOT NULL) STRICT;
CREATE TABLE document_macro_pins (
    document TEXT NOT NULL,revision INTEGER NOT NULL,project_revision INTEGER NOT NULL,
    PRIMARY KEY(document,revision),
    FOREIGN KEY(document,revision) REFERENCES revisions(document_id,number)) STRICT;
CREATE TABLE response_macro_anchors (
    source TEXT PRIMARY KEY,anchors TEXT NOT NULL) STRICT;
CREATE TABLE response_macro_revisions (
    source TEXT NOT NULL,body_revision INTEGER NOT NULL,project_revision INTEGER NOT NULL,
    PRIMARY KEY(source,body_revision)) STRICT;
CREATE TABLE math_macro_contexts (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    definitions TEXT NOT NULL,problems TEXT NOT NULL,held INTEGER NOT NULL,
    context_key TEXT NOT NULL) STRICT;
CREATE TABLE review_macro_pins (
    review_id TEXT NOT NULL,finding_id TEXT NOT NULL,revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL,PRIMARY KEY(review_id,finding_id)) STRICT;
CREATE TRIGGER project_macro_revisions_update BEFORE UPDATE ON project_macro_revisions
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER project_macro_revisions_delete BEFORE DELETE ON project_macro_revisions
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER math_macro_pins_update BEFORE UPDATE ON math_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER math_macro_pins_delete BEFORE DELETE ON math_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER document_macro_pins_update BEFORE UPDATE ON document_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER document_macro_pins_delete BEFORE DELETE ON document_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER response_macro_anchors_update BEFORE UPDATE ON response_macro_anchors
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER response_macro_anchors_delete BEFORE DELETE ON response_macro_anchors
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER response_macro_revisions_update BEFORE UPDATE ON response_macro_revisions
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER response_macro_revisions_delete BEFORE DELETE ON response_macro_revisions
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER math_macro_contexts_update BEFORE UPDATE ON math_macro_contexts
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER math_macro_contexts_delete BEFORE DELETE ON math_macro_contexts
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER review_macro_pins_update BEFORE UPDATE ON review_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;
CREATE TRIGGER review_macro_pins_delete BEFORE DELETE ON review_macro_pins
BEGIN SELECT RAISE(ABORT,'macro revisions and pins are immutable'); END;

-- Comment bodies and their math checks (documents/comment_bodies.py)
CREATE TABLE annotation_body_revisions (
    entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id), revision INTEGER NOT NULL,
    history_entry TEXT NOT NULL REFERENCES annotation_entries(entry_id), body TEXT NOT NULL,
    sha256 TEXT NOT NULL, block_map TEXT NOT NULL, author TEXT NOT NULL, owners TEXT NOT NULL,
    next_block INTEGER NOT NULL,next_span INTEGER NOT NULL,
    PRIMARY KEY(entry_id,revision),CHECK(revision>0)) STRICT;
CREATE TABLE comment_math_sources (
    entry_id TEXT PRIMARY KEY REFERENCES annotation_entries(entry_id),
    document TEXT NOT NULL REFERENCES documents(document_id),project TEXT NOT NULL,
    target INTEGER NOT NULL,completed INTEGER NOT NULL DEFAULT 0,
    macro_revision INTEGER NOT NULL DEFAULT 0,retry_requested INTEGER NOT NULL DEFAULT 0,
    blocked TEXT) STRICT;
CREATE TABLE math_comment_jobs (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id),
    macro_revision INTEGER NOT NULL,macro_sha256 TEXT NOT NULL) STRICT;
CREATE TRIGGER annotation_body_revisions_update BEFORE UPDATE ON annotation_body_revisions
BEGIN SELECT RAISE(ABORT,'body revisions are immutable'); END;
CREATE TRIGGER annotation_body_revisions_delete BEFORE DELETE ON annotation_body_revisions
BEGIN SELECT RAISE(ABORT,'body revisions are immutable'); END;

-- Artifacts (artifacts.py)
CREATE TABLE artifact_objects(
    id TEXT PRIMARY KEY,project TEXT NOT NULL,path TEXT NOT NULL,head INTEGER NOT NULL,
    UNIQUE(project,path)) STRICT;
CREATE TABLE artifact_revisions(
    artifact TEXT NOT NULL REFERENCES artifact_objects(id),number INTEGER NOT NULL,
    project TEXT NOT NULL,size INTEGER NOT NULL CHECK(size>=0),record TEXT NOT NULL,
    data BLOB NOT NULL,PRIMARY KEY(artifact,number)) STRICT;
CREATE TABLE artifact_operations(
    project TEXT NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,digest TEXT NOT NULL,
    request TEXT NOT NULL,state TEXT NOT NULL,worker TEXT,receipt TEXT,
    PRIMARY KEY(project,actor,operation)) STRICT;
CREATE TABLE artifact_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,record TEXT NOT NULL) STRICT;
CREATE INDEX artifact_events_project ON artifact_events(project,seq);
CREATE INDEX artifact_revisions_project ON artifact_revisions(project);
CREATE TRIGGER artifact_revisions_no_update BEFORE UPDATE ON artifact_revisions
BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER artifact_revisions_no_delete BEFORE DELETE ON artifact_revisions
BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER artifact_events_no_update BEFORE UPDATE ON artifact_events
BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER artifact_events_no_delete BEFORE DELETE ON artifact_events
BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;

-- Consistency reviews and findings (consistency/store.py, findings.py)
CREATE TABLE review_windows (
    project TEXT NOT NULL,session TEXT NOT NULL,watermark INTEGER NOT NULL DEFAULT 0,
    document_cursor INTEGER NOT NULL DEFAULT 0,last_dispatch REAL,retry_end INTEGER,
    failures INTEGER NOT NULL DEFAULT 0,requested INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(project,session)) STRICT;
CREATE TABLE review_artifact_cursors (
    project TEXT NOT NULL,session TEXT NOT NULL,cursor INTEGER NOT NULL,
    PRIMARY KEY(project,session)) STRICT;
CREATE TABLE review_config (
    project TEXT NOT NULL,number INTEGER NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,
    PRIMARY KEY(project,number)) STRICT;
CREATE TABLE review_feed (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,session TEXT NOT NULL,
    source_key TEXT NOT NULL,kind TEXT NOT NULL,locator TEXT NOT NULL,payload TEXT NOT NULL,
    sha256 TEXT NOT NULL,complete INTEGER NOT NULL,characters INTEGER NOT NULL,
    structural INTEGER NOT NULL,artifacts INTEGER NOT NULL,cells INTEGER NOT NULL,
    effects INTEGER NOT NULL,created REAL NOT NULL,UNIQUE(project,session,source_key)) STRICT;
CREATE INDEX review_feed_scope ON review_feed(project,session,seq);
CREATE TABLE review_runs (
    review_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,kind TEXT NOT NULL,
    author_run TEXT NOT NULL,owner TEXT NOT NULL,state TEXT NOT NULL,start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL,created REAL NOT NULL,ended REAL,lease_until REAL,
    alias TEXT,resolved_model TEXT,rubric TEXT NOT NULL,config TEXT NOT NULL,
    config_revision TEXT NOT NULL,classifier TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
    cap_reached INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,estimated_tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,coverage_complete INTEGER NOT NULL,
    confirmation_key TEXT) STRICT;
CREATE UNIQUE INDEX review_one_running
    ON review_runs(project,session) WHERE state='running';
CREATE TABLE review_evidence (
    evidence_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    ordinal INTEGER NOT NULL,kind TEXT NOT NULL,locator TEXT NOT NULL,payload TEXT NOT NULL,
    sha256 TEXT NOT NULL,complete INTEGER NOT NULL,UNIQUE(review_id,ordinal)) STRICT;
CREATE TABLE review_reads (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    evidence_id TEXT NOT NULL REFERENCES review_evidence(evidence_id),locator TEXT NOT NULL,
    sha256 TEXT NOT NULL,created REAL NOT NULL) STRICT;
CREATE TABLE review_confirmation_targets (
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),finding_id TEXT NOT NULL,
    revision INTEGER NOT NULL,PRIMARY KEY(review_id,finding_id)) STRICT;
CREATE TABLE review_grants (
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    evidence_id TEXT NOT NULL REFERENCES review_evidence(evidence_id),
    ordinal INTEGER NOT NULL,PRIMARY KEY(review_id,evidence_id),
    UNIQUE(review_id,ordinal)) STRICT;
CREATE TABLE review_calls (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,review_id TEXT NOT NULL,
    created REAL NOT NULL,input_tokens INTEGER,output_tokens INTEGER,resolved_model TEXT,
    estimated_tokens INTEGER NOT NULL,answered INTEGER NOT NULL DEFAULT 0,
    prompt_provenance TEXT) STRICT;
CREATE TABLE review_records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL) STRICT;
CREATE TABLE review_findings (
    finding_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,review_id TEXT,
    severity TEXT NOT NULL,category TEXT NOT NULL,statement TEXT NOT NULL,anchors TEXT NOT NULL,
    remedy TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,reflags INTEGER NOT NULL DEFAULT 0,
    dedupe TEXT NOT NULL,supersedes TEXT,created REAL NOT NULL,
    origin TEXT NOT NULL DEFAULT 'review',math_check_id TEXT REFERENCES math_jobs(check_id),
    CHECK((origin='review' AND review_id IS NOT NULL AND math_check_id IS NULL AND category!='math')
       OR (origin='math' AND review_id IS NULL AND math_check_id IS NOT NULL
           AND category='math' AND severity='warn'))) STRICT;
CREATE INDEX review_findings_scope ON review_findings(project,session,created);
CREATE TABLE review_transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL REFERENCES review_findings(finding_id),
    revision INTEGER NOT NULL,status TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
    payload TEXT NOT NULL,created REAL NOT NULL,UNIQUE(finding_id,revision)) STRICT;
CREATE TABLE review_operations (
    project TEXT NOT NULL,actor TEXT NOT NULL,operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,result TEXT NOT NULL,
    PRIMARY KEY(project,actor,operation_id)) STRICT;
CREATE TABLE review_notices (
    finding_id TEXT NOT NULL,revision INTEGER NOT NULL,project TEXT NOT NULL,
    session TEXT NOT NULL,
    claimed_run TEXT,claim_until REAL,delivered_run TEXT,
    PRIMARY KEY(finding_id,revision)) STRICT;
CREATE TABLE review_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,session TEXT NOT NULL,
    kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL) STRICT;
CREATE TRIGGER review_feed_update BEFORE UPDATE ON review_feed
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_feed_delete BEFORE DELETE ON review_feed
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_evidence_update BEFORE UPDATE ON review_evidence
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_evidence_delete BEFORE DELETE ON review_evidence
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_grants_update BEFORE UPDATE ON review_grants
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_grants_delete BEFORE DELETE ON review_grants
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_confirmation_targets_update BEFORE UPDATE ON review_confirmation_targets
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_confirmation_targets_delete BEFORE DELETE ON review_confirmation_targets
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_reads_update BEFORE UPDATE ON review_reads
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_reads_delete BEFORE DELETE ON review_reads
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_records_update BEFORE UPDATE ON review_records
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_records_delete BEFORE DELETE ON review_records
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_transitions_update BEFORE UPDATE ON review_transitions
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_transitions_delete BEFORE DELETE ON review_transitions
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_config_update BEFORE UPDATE ON review_config
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_config_delete BEFORE DELETE ON review_config
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_events_update BEFORE UPDATE ON review_events
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_events_delete BEFORE DELETE ON review_events
BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END;
CREATE TRIGGER review_calls_prompt_immutable BEFORE UPDATE OF prompt_provenance ON review_calls
BEGIN SELECT RAISE(ABORT, 'review prompt input is immutable'); END;

-- Responses to findings and their math checks (consistency/response_bodies.py)
CREATE TABLE review_response_bodies (
    source TEXT NOT NULL, finding_id TEXT NOT NULL, decision_revision INTEGER NOT NULL,
    revision INTEGER NOT NULL,body TEXT NOT NULL,sha256 TEXT NOT NULL,
    block_map TEXT NOT NULL,author TEXT NOT NULL,owners TEXT NOT NULL,
    next_block INTEGER NOT NULL,next_span INTEGER NOT NULL,
    identity_floor INTEGER NOT NULL,held TEXT,
    PRIMARY KEY(source,revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision),
    CHECK(revision>0)) STRICT;
CREATE TABLE response_math_sources (
    source TEXT PRIMARY KEY,finding_id TEXT NOT NULL,decision_revision INTEGER NOT NULL,
    project TEXT NOT NULL,session TEXT NOT NULL,target INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,retry_requested INTEGER NOT NULL DEFAULT 0,
    blocked TEXT,UNIQUE(finding_id,decision_revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT;
CREATE TABLE review_response_discovery (
    transition_seq INTEGER PRIMARY KEY,finding_id TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,reason TEXT NOT NULL,
    UNIQUE(finding_id,decision_revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT;
CREATE TABLE review_response_pins (
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),finding_id TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,body_revision INTEGER NOT NULL,sha256 TEXT NOT NULL,
    PRIMARY KEY(review_id,finding_id),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT;
CREATE TABLE math_response_jobs (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    source TEXT NOT NULL REFERENCES response_math_sources(source)) STRICT;
CREATE VIEW math_job_scope AS
    SELECT j.check_id,s.project,NULL AS session FROM math_jobs j
    JOIN math_sources s USING(document)
    UNION ALL SELECT j.check_id,f.project,f.session FROM math_jobs j JOIN math_response_jobs r
    USING(check_id) JOIN response_math_sources s ON s.source=r.source JOIN review_findings f
    ON f.finding_id=s.finding_id WHERE j.document IS NULL AND s.project=f.project
    AND s.session=f.session;
CREATE TRIGGER review_response_bodies_update BEFORE UPDATE ON review_response_bodies
BEGIN SELECT RAISE(ABORT,'response source and pins are immutable'); END;
CREATE TRIGGER review_response_bodies_delete BEFORE DELETE ON review_response_bodies
BEGIN SELECT RAISE(ABORT,'response source and pins are immutable'); END;
CREATE TRIGGER review_response_pins_update BEFORE UPDATE ON review_response_pins
BEGIN SELECT RAISE(ABORT,'response source and pins are immutable'); END;
CREATE TRIGGER review_response_pins_delete BEFORE DELETE ON review_response_pins
BEGIN SELECT RAISE(ABORT,'response source and pins are immutable'); END;

-- The current check of each math source, documents, comments and responses (mathjobs.py)
CREATE VIEW math_current_checks AS
    SELECT j.check_id,j.document AS source,j.revision FROM math_jobs j
    JOIN documents d ON d.document_id=j.document
    WHERE NOT EXISTS(SELECT 1 FROM math_comment_jobs c WHERE c.check_id=j.check_id)
    AND d.head=j.revision AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k
    WHERE k.document=j.document AND k.revision=d.head AND NOT EXISTS
    (SELECT 1 FROM math_comment_jobs c WHERE c.check_id=k.check_id))
    UNION ALL
    SELECT j.check_id,'comment:'||c.entry_id,j.revision FROM math_jobs j
    JOIN math_comment_jobs c USING(check_id) JOIN documents d ON d.document_id=j.document
    JOIN comment_math_sources s ON s.entry_id=c.entry_id
    JOIN annotation_body_revisions b ON b.entry_id=c.entry_id AND b.revision=j.revision
    JOIN annotation_entries original ON original.entry_id=c.entry_id
    JOIN annotation_threads t ON t.thread_id=original.thread_id
    WHERE t.document_id=j.document AND s.document=j.document AND s.project=d.project_id
    AND original.kind IN ('create','reply')
    AND s.target=j.revision AND d.head=c.macro_revision AND b.history_entry=(
      SELECT e.entry_id FROM annotation_entries e WHERE e.thread_id=original.thread_id
      AND (e.entry_id=original.entry_id OR (e.kind='edit' AND original.number=1)
      OR (e.kind='edit_body' AND json_extract(e.payload,'$.entry')=original.entry_id))
      ORDER BY e.number DESC LIMIT 1)
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_comment_jobs x
    USING(check_id) WHERE x.entry_id=c.entry_id AND k.revision=j.revision
    AND x.macro_revision=c.macro_revision) UNION ALL
    SELECT j.check_id,r.source,j.revision FROM math_jobs j JOIN math_response_jobs r
    USING(check_id) JOIN response_math_sources s ON s.source=r.source
    JOIN review_findings f ON f.finding_id=s.finding_id
    JOIN review_response_bodies b ON b.source=s.source AND b.revision=j.revision
    WHERE j.document IS NULL AND f.origin='review' AND s.project=f.project AND s.session=f.session
    AND b.finding_id=s.finding_id AND b.decision_revision=s.decision_revision
    AND s.target=j.revision AND j.source_sha256=b.sha256
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_response_jobs x USING(check_id)
                WHERE x.source=r.source AND k.revision=j.revision);

-- Session plans (session_plans.py)
CREATE TABLE session_plan_records(
    project TEXT NOT NULL,session TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
    number INTEGER NOT NULL,key TEXT NOT NULL,record TEXT NOT NULL,bytes INTEGER NOT NULL,
    PRIMARY KEY(project,session,kind,id,number)) STRICT;
CREATE INDEX session_plan_keys ON session_plan_records(project,session,kind,id,key,number);
CREATE TABLE session_plan_projections(
    project TEXT NOT NULL,session TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
    record TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(project,session,kind,id)) STRICT;
CREATE TRIGGER session_plan_records_no_update BEFORE UPDATE ON session_plan_records
BEGIN SELECT RAISE(ABORT,'session-plan history is immutable'); END;
CREATE TRIGGER session_plan_records_no_delete BEFORE DELETE ON session_plan_records
BEGIN SELECT RAISE(ABORT,'session-plan history is immutable'); END;

-- Job notices, waits and watcher calls (jobstate.py)
CREATE TABLE job_runs (
    project TEXT, session TEXT, run TEXT, heartbeat REAL,
    PRIMARY KEY(project, session, run));
CREATE TABLE job_origins (
    project TEXT, session TEXT, job TEXT, run TEXT,
    PRIMARY KEY(project, session, job));
CREATE TABLE job_acknowledged (
    project TEXT, session TEXT, job TEXT, run TEXT,
    PRIMARY KEY(project, session, job));
CREATE TABLE job_waits (
    project TEXT, session TEXT, run TEXT, payload TEXT,
    PRIMARY KEY(project, session, run));
CREATE TABLE job_watcher_calls (
    id INTEGER PRIMARY KEY, project TEXT, session TEXT, run TEXT,
    job TEXT, alias TEXT, requested_at REAL, input TEXT,
    outcome TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    prompt_provenance TEXT);
