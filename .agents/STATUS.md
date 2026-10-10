# Status

Updated: 2026-10-10.

**Phase:** 1 (babbage), milestone M0. **Focus:** infrastructure track P, starting with the project policy library.

## Recent progress

- Policy library (`packages/policy`): schema, safe YAML loading, request classification, hard floor, pool ceiling, evaluation and monitors, with tests and CI. Pull request open for review.
- Development conventions, uv workspace and CI (lint, types, tests, license check).
- Plan snapshot tags pushed.

## Next

- Policy compiler to OpenShell v0.1.3 policy, using the [OpenShell policy investigation](docs/agent-reports/openshell-policy-v0.1.3.md).
- W0 behavior specifications.
- Install OpenShell on babbage and run its kernel qualification.

## Blockers and open questions

- Confirm D68 (policy evaluation details) and PSF-2.0 in tier 1.
- OpenShell's VM driver README calls the driver experimental at v0.1.3, while the support matrix lists it as supported. babbage's planned tier 3 isolation depends on it (D43).
- babbage enforces Ubuntu's AppArmor restriction on unprivileged user namespaces. Shells started from Claude Desktop run under its `claude-desktop` profile and are exempt, so qualification checks must run from a plain terminal or `systemd-run --user`.
- crypto-analysis-skills: confirm the copyright holder before merging its MIT LICENSE pull request.
