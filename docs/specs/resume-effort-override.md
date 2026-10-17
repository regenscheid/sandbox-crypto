# Explicit operator reasoning effort on Resume

Owner-approved 2026-10-17; implementation follows the per-role model profile work.
Ordinary recovery replays recorded model profiles. An authenticated operator may
explicitly change requested reasoning effort for new attempts in one selected
research tree without changing aliases, API kinds, original blueprints or history.
This changes a model request setting, not policy, capabilities or resource ceilings.

## Admission and audit

`POST /api/v0/projects/{project}/sessions/{session}/resume` retains `{}` behavior.
Its bounded optional fields are `root` (an existing root work identity),
`reasoning_effort` (a nonempty map of caller roles to effort strings) and `reason`.
An override requires an explicit nonblank operator reason, at most 2,000 characters;
a reason without an override is refused. The role set is `main`, `delegated`,
`reviewer`, `watcher`, `fold`, `overview`, `skills_metadata`. Each value uses the
model-profile effort contract: a lowercase letter followed by at most 31 lowercase
letters, digits, underscores or hyphens. Null effort values, unknown roles and extra fields are
refused. Provider-specific support is not inferred: an unsupported effort remains
a provider error, never an automatic fallback or silently removed setting.

The existing login and same-origin write boundary applies. Model/tool arguments
cannot choose overrides or operator identity. The selected root must be unfinished
and not explicitly stopped. An override without eligible research work is refused;
it never falls through to legacy held-message requeue and loses the operator choice.

One control transaction inserts the Resume request and its write-once scoped
`research_observations` audit: kind `resume_effort`, identity the Resume request,
work the selected root, and detail containing the effort map, reason and trusted
operator identity. Existing bounded JSON observation storage suffices; no schema
migration is required. Repeated queued/running requests return the same request
only when their root selection and override audit agree exactly. Adding, removing
or changing an override, reason or operator conflicts. In particular, `{}` conflicts
with an already pending Resume carrying an override. A different selected root
retains the existing busy refusal. A fresh request after settlement has a new audit.

## Application

The recovery helper checks the exact running Resume, its current session root and
epoch, recorded Resume lease and live lease identity, and the work's expected
current attempt and waiting state before reading the audit. Stop, replacement
ownership or a later Resume invalidates an old helper call. Recovery's existing
dispatch fences still guard subsequent effects; this helper grants none.

Only reasoning effort is replaced in profiles already present for that attempt.
Disabled roles stay absent: their requested overrides are dormant no-ops retained
in the audit, not claims that a caller ran. Missing original aliases or API pins
are not manufactured by this helper. New attempts retain their effective profile
pins; later ordinary recovery replays those pins. No historical attempt, original
blueprint or deployment-wide setting is rewritten. Changing effort after a Resume
is queued requires settling that request and making a new explicit request.
