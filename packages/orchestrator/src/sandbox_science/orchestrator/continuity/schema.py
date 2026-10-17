# SPDX-License-Identifier: MIT
"""Append-only migration for durable work and explicit Resume ownership."""

from sandbox_science.orchestrator.control.migrations import Schema


def install(schema: Schema) -> None:
    """Schema 62: identities, immutable attempt ancestry and durable recovery requests."""
    schema.execute("""CREATE TABLE research_sessions (
        project TEXT NOT NULL, session TEXT NOT NULL, root TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK(epoch>0), PRIMARY KEY(project,session),
        FOREIGN KEY(project,session) REFERENCES sessions(project_id,session_id)
    ) STRICT""")
    schema.execute("""CREATE TABLE research_work (
        project TEXT NOT NULL, session TEXT NOT NULL, work TEXT NOT NULL,
        root TEXT NOT NULL, parent TEXT, current TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('main','delegated')), kernel TEXT,
        state TEXT NOT NULL CHECK(state IN
            ('running','waiting','paused','blocked','completed','stopped')),
        reason TEXT CHECK(reason IS NULL OR length(reason)<=1000),
        PRIMARY KEY(project,session,work),
        FOREIGN KEY(project,session) REFERENCES research_sessions(project,session),
        CHECK((parent IS NULL)=(role='main'))
    ) STRICT""")
    schema.execute("CREATE INDEX research_work_tree ON research_work(project,session,root,work)")
    schema.execute(
        "CREATE INDEX research_work_parent ON research_work(project,session,parent,state)"
    )
    schema.execute("""CREATE TABLE research_attempts (
        project TEXT NOT NULL, session TEXT NOT NULL, run TEXT NOT NULL, work TEXT NOT NULL,
        predecessor TEXT, epoch INTEGER NOT NULL CHECK(epoch>0), outcome TEXT,
        PRIMARY KEY(project,session,run),
        FOREIGN KEY(project,session,work) REFERENCES research_work(project,session,work)
    ) STRICT""")
    schema.execute(
        "CREATE INDEX research_attempt_chain ON research_attempts(project,session,work,run)"
    )
    schema.execute("""CREATE TRIGGER research_attempt_identity BEFORE UPDATE ON research_attempts
        WHEN old.project!=new.project OR old.session!=new.session OR old.run!=new.run
        OR old.work!=new.work OR old.predecessor IS NOT new.predecessor OR old.epoch!=new.epoch
        OR old.outcome IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'attempt identity and terminal outcome are immutable'); END""")
    schema.execute("""CREATE TABLE research_resumes (
        project TEXT NOT NULL, session TEXT NOT NULL, request TEXT NOT NULL, root TEXT NOT NULL,
        created REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','finished','blocked')),
        reason TEXT CHECK(reason IS NULL OR length(reason)<=1000),
        PRIMARY KEY(project,session,request),
        FOREIGN KEY(project,session,root) REFERENCES research_work(project,session,work)
    ) STRICT""")
    schema.execute(
        "CREATE UNIQUE INDEX research_resume_one ON research_resumes(project,session) "
        "WHERE status IN ('queued','running')"
    )
    schema.execute("""CREATE TABLE research_observations (
        project TEXT NOT NULL, session TEXT NOT NULL, work TEXT NOT NULL, kind TEXT NOT NULL,
        identity TEXT NOT NULL, observed REAL NOT NULL, detail TEXT NOT NULL
        CHECK(json_valid(detail) AND length(CAST(detail AS BLOB))<=16384),
        PRIMARY KEY(project,session,work,kind,identity),
        FOREIGN KEY(project,session,work) REFERENCES research_work(project,session,work)
    ) STRICT""")
