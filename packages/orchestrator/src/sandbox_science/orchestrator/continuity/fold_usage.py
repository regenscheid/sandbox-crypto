# SPDX-License-Identifier: MIT
"""Shared precedence for durable fold usage and its legacy copies."""

from .reservations import UNRESERVED


def fold_copy(
    value: str,
    project_col: str,
    session: str,
    run: str,
    source: str,
    position: str,
    event_seq: str = "0",
) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM research_model_calls m "  # noqa: S608 - fixed columns
        f"WHERE m.project={project_col} AND m.session={session} AND m.run={run} "
        f"AND m.identity=json_extract({value},'$.intent_id') AND m.kind='fold' "
        f"AND (m.usage IS NOT NULL OR (json_extract({value},'$.cost') IS NULL "
        f"AND NOT ({UNRESERVED})))) "
        "AND NOT EXISTS (SELECT 1 FROM research_observations link "
        "JOIN research_model_calls m ON m.project=link.project "
        "AND m.session=link.session "
        "AND m.run=json_extract(link.detail,'$.resolution.run') "
        "AND m.identity=json_extract(link.detail,'$.resolution.identity') "
        "JOIN research_attempts a ON a.project=m.project AND a.session=m.session "
        "AND a.work=link.work AND a.run=m.run "
        f"WHERE link.project={project_col} AND link.session={session} "
        "AND link.kind='resolution:fold_estimate_link' "
        f"AND m.run={run} AND m.kind='fold' AND m.usage IS NULL "
        f"AND json_extract(link.detail,'$.resolution.source')='{source}' "
        f"AND json_extract(link.detail,'$.resolution.position')={position} "
        f"AND coalesce(json_extract(link.detail,'$.resolution.event_seq'),0)={event_seq} "
        f"AND json_extract(link.detail,'$.resolution.estimate_json')={value} "
        f"AND NOT ({UNRESERVED}))"
    )
