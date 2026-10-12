# SPDX-License-Identifier: MIT
"""Job-specific policy resource caps are strict and cannot exceed seven days."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from sandbox_science.policy import PoolCeiling, effective_resources, parse_policy
from sandbox_science.policy.model import Resources


@pytest.mark.parametrize(
    "fields",
    [
        {"job_wall_time_default": "0s"},
        {"job_wall_time_max": "8d"},
        {"job_wall_time_default": "8d"},
        {"jobs_per_session": 0},
        {"jobs_per_environment": -1},
        {"jobs_per_environment": True},
        {"jobs_per_session": "4"},
    ],
)
def test_job_resources_refuse_invalid_limits(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Resources.model_validate(fields)


def test_job_caps_clamp_project_resources_and_omitted_defaults() -> None:
    policy = parse_policy(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: jobs}\n"
        "resources: {job_wall_time_default: 2d, job_wall_time_max: 7d, "
        "jobs_per_session: 4, jobs_per_environment: 8}\n"
    ).policy
    cap = PoolCeiling(
        resources=Resources(
            job_wall_time_default=timedelta(hours=1),
            job_wall_time_max=timedelta(days=2),
            jobs_per_session=2,
            jobs_per_environment=3,
        )
    )
    effective, notices = effective_resources(policy, cap)
    assert effective.job_wall_time_default == timedelta(hours=1)
    assert effective.job_wall_time_max == timedelta(days=2)
    assert effective.jobs_per_session == 2
    assert effective.jobs_per_environment == 3
    assert len(notices) == 4
    omitted = parse_policy(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: jobs}\n"
    ).policy
    effective, notices = effective_resources(omitted, cap)
    assert effective.job_wall_time_default == timedelta(hours=1)
    assert effective.job_wall_time_max == timedelta(days=2)
    assert effective.jobs_per_session == 2
    assert not notices
