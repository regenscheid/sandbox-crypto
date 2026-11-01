# SPDX-License-Identifier: MIT
"""A bounded acknowledgement outbox committed with a retained artifact receipt."""

from __future__ import annotations

from .control.migrations import Schema


def install(schema: Schema) -> None:
    """Append one immutable intent per original publication, retaining delivery audit."""
    for statement in (
        """CREATE TABLE artifact_retention_outbox (
    sequence INTEGER PRIMARY KEY,
    project TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
    endpoint TEXT NOT NULL CHECK(length(endpoint)=64 AND endpoint NOT GLOB '*[^0-9a-f]*'),
    acknowledgement TEXT NOT NULL CHECK(json_valid(acknowledgement)
        AND length(CAST(acknowledgement AS BLOB))<=16384),
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 4611686018427387903),
    last_error TEXT NOT NULL DEFAULT '' CHECK(length(CAST(last_error AS BLOB))<=256),
    UNIQUE(project,actor,operation),
    FOREIGN KEY(project,actor,operation) REFERENCES artifact_operations(project,actor,operation)
) STRICT""",
        "CREATE INDEX artifact_retention_pending ON artifact_retention_outbox "
        "(project,endpoint,delivered,sequence)",
        """CREATE TRIGGER artifact_retention_kept BEFORE UPDATE ON artifact_retention_outbox
WHEN NEW.sequence IS NOT OLD.sequence OR NEW.project IS NOT OLD.project
OR NEW.actor IS NOT OLD.actor OR NEW.operation IS NOT OLD.operation
OR NEW.endpoint IS NOT OLD.endpoint OR NEW.acknowledgement IS NOT OLD.acknowledgement
OR OLD.delivered=1 OR NEW.attempts<OLD.attempts
BEGIN SELECT RAISE(ABORT,'artifact retention intent is kept'); END""",
        """CREATE TRIGGER artifact_retention_no_delete BEFORE DELETE ON artifact_retention_outbox
BEGIN SELECT RAISE(ABORT,'artifact retention intent is kept'); END""",
        # Charge the immutable body plus fixed headroom for bounded status fields.
        """CREATE TRIGGER artifact_retention_usage AFTER INSERT ON artifact_retention_outbox
BEGIN
INSERT OR IGNORE INTO artifact_usage(project) VALUES (NEW.project),('');
UPDATE artifact_usage SET metadata=metadata+length(CAST(NEW.acknowledgement AS BLOB))+1024
WHERE project IN (NEW.project,'');
END""",
    ):
        schema.execute(statement)
