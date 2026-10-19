# SPDX-License-Identifier: MIT
"""Recovery blueprints, planned continuation identity, and paid model dispatch intent."""

from sandbox_science.orchestrator.control.migrations import Schema


def install(schema: Schema) -> None:
    """Schema64 follows immutable collection schema63; never rewrites an old step."""
    schema.execute("""CREATE TABLE research_model_calls (
        project TEXT NOT NULL, session TEXT NOT NULL, run TEXT NOT NULL,
        identity TEXT NOT NULL, kind TEXT NOT NULL, alias TEXT NOT NULL, created REAL NOT NULL,
        usage TEXT CHECK(usage IS NULL OR
            (json_valid(usage) AND length(CAST(usage AS BLOB))<=16384)),
        PRIMARY KEY(project,session,run,identity),
        FOREIGN KEY(project,session,run) REFERENCES research_attempts(project,session,run)
    ) STRICT""")
    schema.execute("""CREATE TRIGGER research_model_identity BEFORE UPDATE ON research_model_calls
        WHEN old.project!=new.project OR old.session!=new.session OR old.run!=new.run
        OR old.identity!=new.identity OR old.kind!=new.kind OR old.alias!=new.alias
        OR old.created!=new.created OR old.usage IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'model intents are immutable after observation'); END""")
    schema.execute("""CREATE TABLE research_blueprints (
        project TEXT NOT NULL, session TEXT NOT NULL, work TEXT NOT NULL,
        snapshot TEXT NOT NULL CHECK(json_valid(snapshot)
            AND length(CAST(snapshot AS BLOB))<=1048576),
        PRIMARY KEY(project,session,work),
        FOREIGN KEY(project,session,work) REFERENCES research_work(project,session,work)
    ) STRICT""")
    schema.execute("""CREATE TABLE research_continuations (
        project TEXT NOT NULL, session TEXT NOT NULL, request TEXT NOT NULL, work TEXT NOT NULL,
        run TEXT NOT NULL, predecessor TEXT NOT NULL, started TEXT NOT NULL,
        PRIMARY KEY(project,session,request,work), UNIQUE(project,session,run),
        FOREIGN KEY(project,session,request) REFERENCES research_resumes(project,session,request),
        FOREIGN KEY(project,session,work) REFERENCES research_work(project,session,work)
    ) STRICT""")
    for table in ("research_blueprints", "research_continuations"):
        schema.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT,'recovery inputs are immutable'); END"
        )
